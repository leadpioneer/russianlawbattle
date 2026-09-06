"""LangGraph-оркестратор прений: супервизор над тремя агентами.

Топология графа::

    START -> init -> build_evidence_pack -> claimant_turn -> defendant_turn
          -> judge_review
    judge_review --(судья: ПРОДОЛЖАТЬ и лимит раундов не исчерпан)--> claimant_turn
    judge_review --(иначе: ЗАВЕРШИТЬ или достигнут max_rounds)------> final_verdict
    final_verdict -> recommendations (блок рекомендаций для target_side) -> END

Узел ``build_evidence_pack`` (этап 3) выполняется ОДИН раз до прений: извлекает
правовые вопросы дела, опрашивает провайдеров (pravo.gov.ru и др.) и собирает
Evidence Pack с честными статусами верификации. Общий Evidence Pack получают
все три роли — каждый интерпретирует его в интересах своей стороны. Если
подтверждённых источников нет, симуляция продолжается в degraded-режиме:
агенты получают инструкцию не утверждать точные нормы как факт.

Судья-супервизор единственный решает, когда прения заканчиваются; ``max_rounds``
из конфига — жёсткий предохранитель. Полная история реплик остаётся в состоянии
графа и возвращается в :class:`DebateResult` для отчёта (report.py).

Протокол событий: узлы публикуют :class:`DebateEvent` (начало реплики, фрагменты
генерации, решение судьи по раунду, готовый вердикт, прогресс правового
исследования) через единый ``sink``-колбэк — консоль (CLI) и WebSocket (веб)
получают один и тот же поток событий. Для обратной совместимости поддержаны
прежние ``on_delta``/``announce``: события конвертируются в них, поэтому
существующий CLI-код не меняется.
"""

from __future__ import annotations

import logging
import operator
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import advisor, claimant_lawyer, defendant_lawyer
from .agents import judge as judge_agent
from .agents.base import ROLE_CLAIMANT, ROLE_DEFENDANT, ROLE_JUDGE, SPEAKER_TITLES, Statement
from .agents.legal_context import VerifiedNorms, build_verified_norms
from .legal_tools import LawExcerpt
from .legal.evidence_pack import build_evidence_pack_async
from .legal.issue_extractor import LegalIssues
from .legal.models import EvidencePack, LegalSource
from .legal.prompts import format_evidence_block
from .config import Config, load_config
from .document_loader import CaseMaterials, load_case
from .llm_client import (
    get_usage_log,
    reset_usage_log,
    reset_clients,
    set_config,
)

logger = logging.getLogger(__name__)

#: Колбэк потокового вывода: принимает очередной фрагмент текста агента.
OutputCallback = Callable[[str], None]

#: Колбэк объявления спикера: (название роли, номер раунда). Legacy-интерфейс CLI.
SpeakerAnnouncer = Callable[[str, int], None]

# --- протокол событий --------------------------------------------------------

#: Типы событий (строки — для прямой сериализации в JSON для WebSocket).
EVENT_AGENT_START = "agent_start"  # агент начинает реплику (role, speaker_title, round, model)
EVENT_DELTA = "delta"  # фрагмент генерируемого текста (role, round, text)
EVENT_AGENT_END = "agent_end"  # реплика готова (role, round, text — полный текст)
EVENT_JUDGE_DECISION = "judge_decision"  # решение судьи по раунду (continues, addressee)
EVENT_RECOMMENDATIONS_DONE = "recommendations_done"  # блок рекомендаций готов (payload: target_side, prospects)
EVENT_VERDICT_DONE = "verdict_done"  # итоговое решение вынесено
EVENT_DEBATE_DONE = "debate_done"  # симуляция завершена (payload: статистика)
EVENT_ERROR = "error"  # фатальная ошибка симуляции (payload: message) — публикует бэкенд при сбое
# Правовой research layer (этап 3):
EVENT_LEGAL_RESEARCH_STARTED = "legal_research_started"  # начато исследование права
EVENT_PROVIDER_STATUS = "provider_status"  # статус провайдера (payload: provider, status, message)
EVENT_LEGAL_SOURCE_FOUND = "legal_source_found"  # найден источник (payload: source_id, verification_status, citation)
EVENT_EVIDENCE_PACK_READY = "evidence_pack_ready"  # pack готов (payload: verified_count, partial_count, warning_count)


@dataclass(frozen=True)
class DebateEvent:
    """Событие симуляции: единый формат для консоли (CLI) и WebSocket (веб).

    Необязательные поля зависят от ``type``; :meth:`as_dict` отдаёт словарь
    без ``None``-полей, готовый к ``json.dumps``.
    """

    type: str
    role: str | None = None  # каноническая роль: claimant_lawyer / defendant_lawyer / judge
    speaker_title: str | None = None  # человекочитаемое название спикера
    round: int | None = None  # номер раунда прений
    model: str | None = None  # модель агента (для EVENT_AGENT_START)
    text: str | None = None  # фрагмент (EVENT_DELTA) или полный текст (EVENT_AGENT_END)
    continues: bool | None = None  # решение судьи: продолжать ли прения
    addressee: str | None = None  # решение судьи: кому адресован вопрос
    payload: dict | None = None  # доп. данные события (статистика и т.п.)

    def as_dict(self) -> dict:
        """Словарь события без ``None``-полей — готов к ``json.dumps``."""
        return {key: value for key, value in asdict(self).items() if value is not None}


#: Подписчик на события: пишет в stdout (CLI) или в WebSocket-очередь (веб).
EventSink = Callable[[DebateEvent], None]

#: Имена узлов графа.
NODE_INIT = "init"
NODE_EXTRACT_ISSUES = "extract_issues"
NODE_RESEARCH = "build_evidence_pack"
NODE_CLAIMANT = "claimant_turn"
NODE_DEFENDANT = "defendant_turn"
NODE_JUDGE = "judge_review"
NODE_VERDICT = "final_verdict"
NODE_RECOMMENDATIONS = "recommendations"


def _norms_warning(norms: VerifiedNorms, role: str, round_number: int) -> list[str]:
    """Предупреждение в состояние графа, если нормы не удалось подтвердить."""
    if not norms.degraded:
        return []
    return [
        f"раунд {round_number}, {SPEAKER_TITLES.get(role, role)}: нормы права не подтверждены "
        "внешним источником (MCP pravo.gov.ru недоступен или не дал результатов)"
    ]


def _pack_to_excerpts(pack: EvidencePack | None) -> list[LawExcerpt]:
    """Конвертация источников Evidence Pack в LawExcerpt (для отчёта, этап 2).

    Совместимость: отчёт и API ожидают LawExcerpt; статусы верификации этапа 3
    теряются в этой проекции (полные данные — в evidence_pack отчёта).
    """
    if pack is None:
        return []
    return [
        LawExcerpt(
            eid=source.official_url or source.id,
            title=source.title,
            act_type=source.source_type,
            number="",
            date=source.effective_date or "",
            source_url=source.official_url or "",
            text=source.excerpt,
            text_available=bool(source.excerpt),
            verified=source.verified,
        )
        for source in pack.sources
    ]


def _pack_warnings(pack: EvidencePack | None, role: str, round_number: int) -> list[str]:
    """Предупреждения Evidence Pack (degraded-режим) для состояния графа."""
    if pack is None or pack.verified_sources:
        return []
    return [
        f"{SPEAKER_TITLES.get(role, role)} (раунд {round_number}): подтверждённых "
        "правовых источников нет; ссылки на нормы требуют ручной проверки"
    ]


class DebateState(TypedDict, total=False):
    """Состояние процесса (in-memory, узлы возвращают частичные обновления)."""

    cfg: Config
    materials: object  # CaseMaterials (in-memory объект)
    history: Annotated[list[Statement], operator.add]  # реплики накапливаются
    round_number: int
    judge_decision: judge_agent.JudgeDecision | None
    verdict: str
    target_side: str  # чья сторона нужна для рекомендаций: claimant | defendant
    recommendations: advisor.Recommendation | None  # блок рекомендаций (последний узел)
    norms_used: Annotated[list[LawExcerpt], operator.add]  # подтверждённые нормы за прогон
    legal_warnings: Annotated[list[str], operator.add]  # предупреждения о неподтверждённых нормах
    stopped: bool  # пользователь нажал «Остановить» (частичный результат)
    # Правовой research layer (этап 3):
    legal_issues: LegalIssues | None  # структурированные вопросы дела
    evidence_pack: EvidencePack | None  # собранный до прений набор источников
    evidence_block: str  # готовый prompt-блок Evidence Pack (для всех агентов)


@dataclass(frozen=True)
class DebateResult:
    """Итог симуляции: конфиг, материалы, полная история реплик и вердикт."""

    cfg: Config
    materials: object  # CaseMaterials
    history: list[Statement]
    verdict: str
    rounds_played: int
    finished_by_judge: bool  # True — судья решил ЗАВЕРШИТЬ; False — достигнут max_rounds
    target_side: str = "claimant"  # сторона, для которой готовились рекомендации
    recommendations: advisor.Recommendation | None = None  # блок рекомендаций (после вердикта)
    verified_norms: tuple[LawExcerpt, ...] = ()  # дедуплицированные нормы за весь прогон
    legal_warning: str | None = None  # предупреждение о неподтверждённых нормах (в отчёт)
    stopped: bool = False  # симуляция остановлена пользователем до финала
    usage_log: tuple[tuple[str, str, Any], ...] = ()  # (роль, модель, TokenUsage) за прогон
    evidence_pack: EvidencePack | None = None  # Evidence Pack прений (этап 3)


def build_graph(
    cfg: Config,
    *,
    sink: EventSink | None = None,
    on_delta: OutputCallback | None = None,
    announce: SpeakerAnnouncer | None = None,
    should_stop: Callable[[], bool] | None = None,
):
    """Собрать и скомпилировать граф прений; вывод замыкается в узлы.

    Вывод можно получать любым способом (или несколькими сразу): ``sink``
    получает :class:`DebateEvent`, legacy-колбэки ``on_delta``/``announce``
    сохраняют прежнее поведение CLI. ``should_stop`` — кооперативная остановка:
    узлы проверяют флаг перед LLM-вызовами и завершают граф досрочно.
    """

    def emit(event: DebateEvent) -> None:
        """Единая точка публикации событий: sink + конвертация в legacy-колбэки."""
        if sink is not None:
            sink(event)
        if announce is not None and event.type == EVENT_AGENT_START:
            announce(event.speaker_title or SPEAKER_TITLES.get(event.role, ""), event.round or 0)
        if on_delta is not None and event.type == EVENT_DELTA:
            on_delta(event.text or "")

    def stopped_now() -> bool:
        """Проверка флага остановки (безопасно при should_stop=None)."""
        return bool(should_stop is not None and should_stop())

    def init(_state: DebateState) -> dict:
        """Узел init: загрузка материалов дела, подготовка состояния."""
        materials = _load_case_cached(cfg)
        logger.info(
            "Узел %s: дело загружено (%d документов, ~%d токенов), max_rounds=%d.",
            NODE_INIT,
            len(materials.fragments),
            materials.estimated_tokens,
            cfg.max_rounds,
        )
        return {"cfg": cfg, "materials": materials, "round_number": 0}

    def legal_research(state: DebateState) -> dict:
        """Узел research: вопросы дела → провайдеры → Evidence Pack (этап 3).

        Выполняется ОДИН раз до прений; общий Evidence Pack получают все три
        роли. Ошибка исследования деградирует (пустой pack + warning), но не
        роняет симуляцию.
        """
        if stopped_now():
            return {"stopped": True}
        emit(DebateEvent(EVENT_LEGAL_RESEARCH_STARTED))
        materials: CaseMaterials = state["materials"]
        free_text = (materials.context or "").strip() or None
        try:
            pack, issues = _run_evidence_pack(
                cfg, materials, free_text_query=free_text, emit=emit
            )
        except Exception as exc:  # noqa: BLE001 — деградация вместо падения симуляции
            logger.warning("Правовое исследование не удалось: %s", exc)
            from .legal.models import now_iso

            pack = EvidencePack(
                case_id="session",
                jurisdiction=cfg.jurisdiction,
                generated_at=now_iso(),
                warnings=[f"правовое исследование не удалось: {type(exc).__name__}: {exc}"],
            )
            issues = LegalIssues(source="fallback")
        block = format_evidence_block(pack)
        logger.info(
            "Узел %s: источников=%d (verified=%d), warnings=%d.",
            NODE_RESEARCH,
            len(pack.sources),
            len(pack.verified_sources),
            len(pack.warnings),
        )
        return {
            "legal_issues": issues,
            "evidence_pack": pack,
            "evidence_block": block,
        }

    def claimant_turn(state: DebateState) -> dict:
        """Реплика юриста заявителя (с общим Evidence Pack)."""
        if stopped_now():
            return {"stopped": True}
        round_number = state["round_number"] + 1
        emit(
            DebateEvent(
                EVENT_AGENT_START,
                role=ROLE_CLAIMANT,
                speaker_title=SPEAKER_TITLES[ROLE_CLAIMANT],
                round=round_number,
                model=cfg.model_for(ROLE_CLAIMANT),
            )
        )
        statement = claimant_lawyer.make_statement(
            state["cfg"],
            state["materials"],
            round_number,
            state.get("history", []),
            norms_block=state.get("evidence_block", ""),
            on_delta=lambda chunk: emit(
                DebateEvent(EVENT_DELTA, role=ROLE_CLAIMANT, round=round_number, text=chunk)
            ),
        )
        emit(
            DebateEvent(
                EVENT_AGENT_END,
                role=ROLE_CLAIMANT,
                speaker_title=SPEAKER_TITLES[ROLE_CLAIMANT],
                round=round_number,
                text=statement.text,
                payload={"usage": statement.text.usage.as_dict()},
            )
        )
        logger.info("Узел %s: реплика %d симв. (раунд %d).", NODE_CLAIMANT, len(statement.text), round_number)
        return {
            "history": [statement],
            "round_number": round_number,
            "norms_used": _pack_to_excerpts(state.get("evidence_pack")),
            "legal_warnings": _pack_warnings(state.get("evidence_pack"), ROLE_CLAIMANT, round_number),
        }

    def defendant_turn(state: DebateState) -> dict:
        """Реплика юриста ответчика (с общим Evidence Pack)."""
        if stopped_now():
            return {"stopped": True}
        round_number = state["round_number"]
        emit(
            DebateEvent(
                EVENT_AGENT_START,
                role=ROLE_DEFENDANT,
                speaker_title=SPEAKER_TITLES[ROLE_DEFENDANT],
                round=round_number,
                model=cfg.model_for(ROLE_DEFENDANT),
            )
        )
        statement = defendant_lawyer.make_statement(
            state["cfg"],
            state["materials"],
            round_number,
            state.get("history", []),
            norms_block=state.get("evidence_block", ""),
            on_delta=lambda chunk: emit(
                DebateEvent(EVENT_DELTA, role=ROLE_DEFENDANT, round=round_number, text=chunk)
            ),
        )
        emit(
            DebateEvent(
                EVENT_AGENT_END,
                role=ROLE_DEFENDANT,
                speaker_title=SPEAKER_TITLES[ROLE_DEFENDANT],
                round=round_number,
                text=statement.text,
                payload={"usage": statement.text.usage.as_dict()},
            )
        )
        logger.info("Узел %s: реплика %d симв. (раунд %d).", NODE_DEFENDANT, len(statement.text), round_number)
        return {
            "history": [statement],
            "norms_used": _pack_to_excerpts(state.get("evidence_pack")),
            "legal_warnings": _pack_warnings(state.get("evidence_pack"), ROLE_DEFENDANT, round_number),
        }

    def judge_review(state: DebateState) -> dict:
        """Оценка судьи по итогам раунда + решение о ходе процесса."""
        if stopped_now():
            return {"stopped": True}
        round_number = state["round_number"]
        emit(
            DebateEvent(
                EVENT_AGENT_START,
                role=ROLE_JUDGE,
                speaker_title=SPEAKER_TITLES[ROLE_JUDGE],
                round=round_number,
                model=cfg.model_for(ROLE_JUDGE),
            )
        )
        statement, decision = judge_agent.review_round(
            state["cfg"],
            state["materials"],
            round_number,
            state.get("history", []),
            on_delta=lambda chunk: emit(
                DebateEvent(EVENT_DELTA, role=ROLE_JUDGE, round=round_number, text=chunk)
            ),
        )
        emit(
            DebateEvent(
                EVENT_AGENT_END,
                role=ROLE_JUDGE,
                speaker_title=SPEAKER_TITLES[ROLE_JUDGE],
                round=round_number,
                text=statement.text,
                payload={"usage": statement.text.usage.as_dict()},
            )
        )
        emit(
            DebateEvent(
                EVENT_JUDGE_DECISION,
                role=ROLE_JUDGE,
                round=round_number,
                continues=decision.continues,
                addressee=decision.addressee,
            )
        )
        logger.info(
            "Узел %s: решение=%s (кому: %s).",
            NODE_JUDGE,
            "ПРОДОЛЖАТЬ" if decision.continues else "ЗАВЕРШИТЬ",
            decision.addressee or "—",
        )
        return {"history": [statement], "judge_decision": decision}

    def should_continue(state: DebateState) -> str:
        """Условный переход после судьи: новый раунд, итоговое решение или стоп."""
        if state.get("stopped"):
            logger.info("Переход: %s -> END (остановлено пользователем).", NODE_JUDGE)
            return "__end__"
        decision = state.get("judge_decision")
        if decision is not None and not decision.continues:
            logger.info("Переход: %s -> %s (судья решил завершить).", NODE_JUDGE, NODE_VERDICT)
            return NODE_VERDICT
        if state["round_number"] >= state["cfg"].max_rounds:
            logger.info(
                "Переход: %s -> %s (достигнут max_rounds=%d).",
                NODE_JUDGE,
                NODE_VERDICT,
                state["cfg"].max_rounds,
            )
            return NODE_VERDICT
        logger.info(
            "Переход: %s -> %s (следующий раунд %d из %d).",
            NODE_JUDGE,
            NODE_CLAIMANT,
            state["round_number"] + 1,
            state["cfg"].max_rounds,
        )
        return NODE_CLAIMANT

    def final_verdict(state: DebateState) -> dict:
        """Итоговое мотивированное решение судьи (с общим Evidence Pack)."""
        if stopped_now():
            return {"stopped": True}
        round_number = state["round_number"]
        title = f"{SPEAKER_TITLES[ROLE_JUDGE]} — итоговое решение"
        emit(
            DebateEvent(
                EVENT_AGENT_START,
                role=ROLE_JUDGE,
                speaker_title=title,
                round=round_number,
                model=cfg.model_for(ROLE_JUDGE),
            )
        )
        verdict = judge_agent.generate_verdict(
            state["cfg"],
            state["materials"],
            state.get("history", []),
            norms_block=state.get("evidence_block", ""),
            on_delta=lambda chunk: emit(
                DebateEvent(EVENT_DELTA, role=ROLE_JUDGE, round=round_number, text=chunk)
            ),
        )
        emit(
            DebateEvent(
                EVENT_AGENT_END,
                role=ROLE_JUDGE,
                speaker_title=title,
                round=round_number,
                text=verdict,
                payload={"usage": verdict.usage.as_dict()},
            )
        )
        emit(
            DebateEvent(
                EVENT_VERDICT_DONE,
                role=ROLE_JUDGE,
                round=round_number,
                payload={"length": len(verdict), "usage": verdict.usage.as_dict()},
            )
        )
        logger.info("Узел %s: решение готово (%d симв.).", NODE_VERDICT, len(verdict))
        return {
            "verdict": verdict,
            "norms_used": _pack_to_excerpts(state.get("evidence_pack")),
            "legal_warnings": _pack_warnings(state.get("evidence_pack"), ROLE_JUDGE, round_number),
        }

    def recommendations_node(state: DebateState) -> dict:
        """Блок рекомендаций для стороны, выбранной пользователем (с Evidence Pack)."""
        if stopped_now():
            return {"stopped": True}
        round_number = state["round_number"]
        emit(
            DebateEvent(
                EVENT_AGENT_START,
                role=ROLE_JUDGE,
                speaker_title=advisor.ADVISOR_TITLE,
                round=round_number,
                model=cfg.model_for(ROLE_JUDGE),
            )
        )
        target_side = state.get("target_side", "claimant")
        rec = advisor.generate_recommendations(
            state["cfg"],
            state["materials"],
            state.get("history", []),
            state.get("verdict", ""),
            target_side,
            norms_block=state.get("evidence_block", ""),
            on_delta=lambda chunk: emit(
                DebateEvent(
                    EVENT_DELTA,
                    role=ROLE_JUDGE,
                    speaker_title=advisor.ADVISOR_TITLE,
                    round=round_number,
                    text=chunk,
                )
            ),
        )
        emit(
            DebateEvent(
                EVENT_AGENT_END,
                role=ROLE_JUDGE,
                speaker_title=advisor.ADVISOR_TITLE,
                round=round_number,
                text=rec.text,
                payload={"usage": rec.text.usage.as_dict()},
            )
        )
        emit(
            DebateEvent(
                EVENT_RECOMMENDATIONS_DONE,
                role=ROLE_JUDGE,
                round=round_number,
                payload={"target_side": rec.target_side, "prospects": rec.prospects},
            )
        )
        logger.info(
            "Узел %s: рекомендации для %s (перспектива: %s).",
            NODE_RECOMMENDATIONS,
            rec.target_side,
            rec.prospects,
        )
        return {
            "recommendations": rec,
            "norms_used": _pack_to_excerpts(state.get("evidence_pack")),
            "legal_warnings": _pack_warnings(state.get("evidence_pack"), ROLE_JUDGE, round_number),
        }

    graph = StateGraph(DebateState)
    graph.add_node(NODE_INIT, init)
    graph.add_node(NODE_RESEARCH, legal_research)
    graph.add_node(NODE_CLAIMANT, claimant_turn)
    graph.add_node(NODE_DEFENDANT, defendant_turn)
    graph.add_node(NODE_JUDGE, judge_review)
    graph.add_node(NODE_VERDICT, final_verdict)
    graph.add_node(NODE_RECOMMENDATIONS, recommendations_node)

    graph.add_edge(START, NODE_INIT)
    graph.add_edge(NODE_INIT, NODE_RESEARCH)
    graph.add_edge(NODE_RESEARCH, NODE_CLAIMANT)
    graph.add_edge(NODE_CLAIMANT, NODE_DEFENDANT)
    graph.add_edge(NODE_DEFENDANT, NODE_JUDGE)
    graph.add_conditional_edges(
        NODE_JUDGE,
        should_continue,
        {NODE_CLAIMANT: NODE_CLAIMANT, NODE_VERDICT: NODE_VERDICT, END: END},
    )
    graph.add_edge(NODE_VERDICT, NODE_RECOMMENDATIONS)
    graph.add_edge(NODE_RECOMMENDATIONS, END)

    logger.info(
        "Граф прений собран: init -> build_evidence_pack -> claimant_turn -> "
        "defendant_turn -> judge_review -> ..."
    )
    return graph.compile()


#: Кэш материалов дела в пределах процесса (ключ — корень проекта).
_case_cache: dict[str, CaseMaterials] = {}


def _dedupe_norms(items: list[LawExcerpt]) -> tuple[LawExcerpt, ...]:
    """Дедупликация норм по eid с сохранением порядка (запросы узлов пересекаются)."""
    seen: set[str] = set()
    ordered: list[LawExcerpt] = []
    for item in items:
        key = item.eid or item.source_url
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return tuple(ordered)


def _join_warnings(warnings: list[str]) -> str | None:
    """Слить предупреждения узлов без повторов; None, если всё чисто."""
    unique = list(dict.fromkeys(warning for warning in warnings if warning))
    return "; ".join(unique) or None


def _load_case_cached(cfg: Config) -> CaseMaterials:
    """Материалы дела с кэшем: за прогон симуляции дело загружается один раз."""
    key = str(cfg.project_root)
    if key not in _case_cache:
        _case_cache[key] = load_case(cfg)
    return _case_cache[key]


def _run_evidence_pack(
    cfg: Config,
    materials: CaseMaterials,
    *,
    free_text_query: str | None = None,
    emit: EventSink,
) -> tuple[EvidencePack, LegalIssues]:
    """Собрать Evidence Pack синхронно (узел графа) с событиями прогресса.

    Извлекает вопросы дела, запускает провайдеры, публикует события
    ``provider_status``/``legal_source_found``/``evidence_pack_ready``.
    Возвращает (pack, issues).
    """
    import asyncio

    from .legal.evidence_pack import build_evidence_pack_async
    from .legal.service import LegalResearchService

    async def _status_forwarder():
        statuses = await service.healthcheck_all()
        for status in statuses:
            emit(
                DebateEvent(
                    EVENT_PROVIDER_STATUS,
                    payload={
                        "provider": status.provider,
                        "status": status.status,
                        "message": status.message,
                    },
                )
            )
        return statuses

    async def _inner() -> tuple[EvidencePack, LegalIssues]:
        service = LegalResearchService()
        await _status_forwarder()
        pack, issues, _result = await build_evidence_pack_async(
            materials.context or "",
            materials.full_context()[:4000],
            cfg.jurisdiction,
            case_id="session",
            free_text_query=free_text_query,
            use_llm=True,
            service=service,
        )
        for source in pack.sources:
            emit(
                DebateEvent(
                    EVENT_LEGAL_SOURCE_FOUND,
                    payload={
                        "source_id": source.id,
                        "verification_status": source.verification_status,
                        "citation": source.citation[:200],
                        "source_type": source.source_type,
                    },
                )
            )
        emit(
            DebateEvent(
                EVENT_EVIDENCE_PACK_READY,
                payload={
                    "verified_count": len(pack.verified_sources),
                    "partial_count": len(pack.partially_verified_sources),
                    "warning_count": len(pack.warnings),
                    "total_sources": len(pack.sources),
                },
            )
        )
        return pack, issues

    return asyncio.run(_inner())


def clear_case_cache() -> None:
    """Сбросить кэш материалов дела (например, если файлы дела изменились)."""
    _case_cache.clear()


def run_debate(
    *,
    cfg: Config | None = None,
    max_rounds: int | None = None,
    target_side: str = "claimant",
    sink: EventSink | None = None,
    on_delta: OutputCallback | None = None,
    announce: SpeakerAnnouncer | None = None,
    should_stop: Callable[[], bool] | None = None,
    config_path: str | None = None,
) -> DebateResult:
    """Запустить симуляцию прений и вернуть итог (история + вердикт).

    :param cfg: готовый конфиг (веб-сессии со своим ``project_root``); если задан,
        ``config_path`` игнорируется. По умолчанию — загрузка из ``config.yaml``.
    :param max_rounds: переопределение лимита раундов из конфига (для быстрых прогонов).
    :param target_side: чья сторона нужна для рекомендаций: ``claimant`` | ``defendant``.
    :param sink: подписчик на события :class:`DebateEvent` (консоль/WebSocket).
    :param on_delta: legacy-колбэк стриминга текста агентов (вывод в консоль).
    :param announce: legacy-колбэк объявления спикера (название роли, номер раунда).
    :param should_stop: колбэк кооперативной остановки (проверяется между LLM-вызовами).
    :param config_path: путь к config.yaml (по умолчанию — config.yaml проекта).
    """
    effective = (
        cfg
        if cfg is not None
        else (load_config(config_path) if config_path else load_config())
    )
    if max_rounds is not None:
        if max_rounds < 1:
            raise ValueError(f"max_rounds должен быть >= 1, получено: {max_rounds}.")
        effective = replace(effective, max_rounds=max_rounds)
    if target_side not in advisor.TARGET_SIDES:
        raise ValueError(
            f"target_side должен быть одним из {advisor.TARGET_SIDES}, получено: {target_side!r}."
        )
    cfg = effective

    reset_clients()
    set_config(cfg)  # клиенты и суммаризатор должны использовать тот же конфиг
    clear_case_cache()
    reset_usage_log()  # накопление расхода токенов только за этот прогон
    logger.info(
        "Старт симуляции: юрисдикция=%s; модели: заявитель=%s, ответчик=%s, судья=%s; max_rounds=%d.",
        cfg.jurisdiction,
        cfg.model_claimant_lawyer,
        cfg.model_defendant_lawyer,
        cfg.model_judge,
        cfg.max_rounds,
    )

    graph = build_graph(
        cfg, sink=sink, on_delta=on_delta, announce=announce, should_stop=should_stop
    )
    final: DebateState = graph.invoke({"target_side": target_side})

    stopped = bool(final.get("stopped"))
    decision = final.get("judge_decision")
    result = DebateResult(
        cfg=cfg,
        materials=final["materials"],
        history=list(final.get("history", [])),
        verdict=final.get("verdict", ""),
        rounds_played=final["round_number"],
        finished_by_judge=bool(decision is not None and not decision.continues and not stopped),
        target_side=target_side,
        recommendations=final.get("recommendations"),
        verified_norms=_dedupe_norms(final.get("norms_used", [])),
        legal_warning=_join_warnings(final.get("legal_warnings", [])),
        stopped=stopped,
        usage_log=tuple(get_usage_log()),
        evidence_pack=final.get("evidence_pack"),
    )
    logger.info(
        "Симуляция завершена: раундов=%d, завершена судьёй=%s, остановлена=%s, реплик=%d, "
        "вердикт=%d симв., вызовов LLM=%d.",
        result.rounds_played,
        result.finished_by_judge,
        stopped,
        len(result.history),
        len(result.verdict),
        len(result.usage_log),
    )
    if sink is not None:
        sink(
            DebateEvent(
                EVENT_DEBATE_DONE,
                payload={
                    "rounds_played": result.rounds_played,
                    "finished_by_judge": result.finished_by_judge,
                    "statements": len(result.history),
                    "verdict_length": len(result.verdict),
                    "recommendations_prospects": (
                        result.recommendations.prospects if result.recommendations else None
                    ),
                    "stopped": stopped,
                    "usage_log": [
                        {"role": role, "model": model, "usage": usage.as_dict()}
                        for role, model, usage in result.usage_log
                    ],
                },
            )
        )
    return result
