"""Хранилище веб-сессий симуляции (в памяти) и фоновый запуск прений.

Каждая сессия — изолированный каталог ``sessions/<id>/`` (case_files/,
case_context.md, output/): конфиг сессии указывает на него через
``Config.project_root``, поэтому document_loader и report работают без правок.
События симуляции накапливаются в списке сессии — WebSocket-клиенты читают их
со своего индекса (переподключение и несколько клиентов поддерживаются).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config, PROJECT_ROOT
from .graph import EVENT_ERROR, DebateEvent, DebateResult, run_debate
from .report import render_report, save_report

logger = logging.getLogger(__name__)

#: Корень всех веб-сессий (gitignore).
SESSIONS_DIR = PROJECT_ROOT / "sessions"

# Статусы жизненного цикла сессии.
STATUS_CREATED = "created"  # настроена, материалы не загружены
STATUS_READY = "ready"  # материалы загружены, можно запускать
STATUS_RUNNING = "running"  # симуляция выполняется
STATUS_DONE = "done"  # завершена успешно
STATUS_ERROR = "error"  # завершена с ошибкой
STATUS_STOPPED = "stopped"  # остановлена пользователем (частичный результат)


@dataclass
class Session:
    """Состояние одной пользовательской сессии."""

    id: str
    config: Config
    target_side: str  # claimant | defendant — для блока рекомендаций (шаг 3)
    created_at: datetime
    status: str = STATUS_CREATED
    events: list[DebateEvent] = field(default_factory=list)
    result: DebateResult | None = None
    report_md: str | None = None
    report_path: Path | None = None
    error: str | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    pricing: dict = field(default_factory=dict)  # model -> ModelPricing (для оценки денег)
    cost_summary: dict | None = None  # сводка токенов/денег после завершения

    def request_stop(self) -> bool:
        """Запросить кооперативную остановку; True — если симуляция была running."""
        if self.status != STATUS_RUNNING:
            return False
        self.stop_event.set()
        return True

    def should_stop(self) -> bool:
        """Колбэк для графа: пора ли останавливаться."""
        return self.stop_event.is_set()

    def public_info(self) -> dict:
        """Данные сессии для API (без событий и тяжёлых объектов)."""
        return {
            "session_id": self.id,
            "status": self.status,
            "target_side": self.target_side,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "jurisdiction": self.config.jurisdiction,
            "max_rounds": self.config.max_rounds,
            "events_received": len(self.events),
            "error": self.error,
        }


class SessionStore:
    """Потокобезопасное in-memory хранилище сессий (однопользовательский MVP)."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, target_side: str, config_factory: Callable[[Path], Config]) -> Session:
        """Создать сессию: каталог генерируется первым, конфиг строится по нему.

        :param config_factory: строит :class:`Config` с ``project_root=каталог сессии``.
        """
        session_id = uuid.uuid4().hex[:12]
        session_dir = SESSIONS_DIR / session_id
        (session_dir / "case_files").mkdir(parents=True, exist_ok=True)
        session = Session(
            id=session_id,
            config=config_factory(session_dir),
            target_side=target_side,
            created_at=datetime.now(),
        )
        with self._lock:
            self._sessions[session_id] = session
        logger.info("Создана сессия %s (каталог %s).", session_id, session_dir)
        return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)


def build_cost_summary(
    result: DebateResult, pricing: dict
) -> dict:
    """Сводка расхода токенов и денег по прогону (деньги — только если есть прайсы)."""
    from .llm_client import EMPTY_USAGE, TokenUsage, estimate_cost

    calls = []
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    total_cost = 0.0
    cost_available = bool(pricing)
    for role, model, usage in result.usage_log:
        usage = usage if isinstance(usage, TokenUsage) else EMPTY_USAGE
        for key in totals:
            totals[key] += getattr(usage, key)
        cost = estimate_cost(model, usage, pricing)
        if cost is None:
            cost_available = False
        else:
            total_cost += cost
        calls.append({"role": role, "model": model, "usage": usage.as_dict(), "cost_usd": cost})
    return {
        "calls": calls,
        "totals": totals,
        "total_cost_usd": round(total_cost, 6) if cost_available else None,
        "cost_available": cost_available,
    }


def run_session_in_thread(session: Session) -> threading.Thread:
    """Запустить симуляцию сессии в фоновом потоке; события пишутся в session.events.

    Поток безопасно глотает все исключения: статус сессии становится ``error``,
    в поток событий добавляется синтетическое событие ``error`` для WebSocket.
    """

    def _target() -> None:
        try:
            session.status = STATUS_RUNNING
            session.stop_event.clear()
            logger.info("Сессия %s: старт симуляции.", session.id)
            result = run_debate(
                cfg=session.config,
                target_side=session.target_side,
                sink=session.events.append,
                should_stop=session.should_stop,
            )
            session.result = result
            session.cost_summary = build_cost_summary(result, session.pricing)
            if result.stopped:
                session.status = STATUS_STOPPED
                logger.info("Сессия %s: остановлена пользователем.", session.id)
                return  # отчёт не генерируем — материалы можно править и перезапустить
            session.report_md = render_report(result)
            session.report_path = save_report(result)
            session.status = STATUS_DONE
            logger.info("Сессия %s: завершена, отчёт %s.", session.id, session.report_path)
        except Exception as exc:  # noqa: BLE001 — любая ошибка не должна ронять сервер
            logger.exception("Сессия %s: ошибка симуляции.", session.id)
            session.error = str(exc)
            session.status = STATUS_ERROR
            session.events.append(
                DebateEvent(type=EVENT_ERROR, payload={"message": str(exc)})
            )

    thread = threading.Thread(target=_target, daemon=True, name=f"debate-{session.id}")
    thread.start()
    return thread
