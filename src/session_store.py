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


def run_session_in_thread(session: Session) -> threading.Thread:
    """Запустить симуляцию сессии в фоновом потоке; события пишутся в session.events.

    Поток безопасно глотает все исключения: статус сессии становится ``error``,
    в поток событий добавляется синтетическое событие ``error`` для WebSocket.
    """

    def _target() -> None:
        try:
            session.status = STATUS_RUNNING
            logger.info("Сессия %s: старт симуляции.", session.id)
            result = run_debate(
                cfg=session.config,
                target_side=session.target_side,
                sink=session.events.append,
            )
            session.result = result
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
