"""LangGraph-оркестратор прений: супервизор над тремя агентами.

Топология графа::

    START -> init -> claimant_turn -> defendant_turn -> judge_review
    judge_review --(судья: ПРОДОЛЖАТЬ и лимит раундов не исчерпан)--> claimant_turn
    judge_review --(иначе: ЗАВЕРШИТЬ или достигнут max_rounds)------> final_verdict -> END

Судья-супервизор единственный решает, когда прения заканчиваются; ``max_rounds``
из конфига — жёсткий предохранитель. Полная история реплик остаётся в состоянии
графа и возвращается в :class:`DebateResult` для отчёта (report.py).

Протокол событий: узлы публикуют :class:`DebateEvent` (начало реплики, фрагменты
генерации, решение судьи по раунду, готовый вердикт) через единый ``sink``-колбэк —
консоль (CLI) и WebSocket (веб) получают один и тот же поток событий. Для обратной
совместимости поддержаны прежние ``on_delta``/``announce``: события конвертируются
в них, поэтому существующий CLI-код не меняется.
"""

from __future__ import annotations

import logging
import operator
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import claimant_lawyer, defendant_lawyer
from .agents import judge as judge_agent
from .agents.base import ROLE_CLAIMANT, ROLE_DEFENDANT, ROLE_JUDGE, SPEAKER_TITLES, Statement
from .config import Config, load_config
from .document_loader import CaseMaterials, load_case
from .llm_client import reset_clients, set_config

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
EVENT_VERDICT_DONE = "verdict_done"  # итоговое решение вынесено
EVENT_DEBATE_DONE = "debate_done"  # симуляция завершена (payload: статистика)


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
NODE_CLAIMANT = "claimant_turn"
NODE_DEFENDANT = "defendant_turn"
NODE_JUDGE = "judge_review"
NODE_VERDICT = "final_verdict"


class DebateState(TypedDict, total=False):
    """Состояние процесса (in-memory, узлы возвращают частичные обновления)."""

    cfg: Config
    materials: object  # CaseMaterials (in-memory объект)
    history: Annotated[list[Statement], operator.add]  # реплики накапливаются
    round_number: int
    judge_decision: judge_agent.JudgeDecision | None
    verdict: str


@dataclass(frozen=True)
class DebateResult:
    """Итог симуляции: конфиг, материалы, полная история реплик и вердикт."""

    cfg: Config
    materials: object  # CaseMaterials
    history: list[Statement]
    verdict: str
    rounds_played: int
    finished_by_judge: bool  # True — судья решил ЗАВЕРШИТЬ; False — достигнут max_rounds


def build_graph(
    cfg: Config,
    *,
    sink: EventSink | None = None,
    on_delta: OutputCallback | None = None,
    announce: SpeakerAnnouncer | None = None,
):
    """Собрать и скомпилировать граф прений; вывод замыкается в узлы.

    Вывод можно получать любым способом (или несколькими сразу): ``sink``
    получает :class:`DebateEvent`, legacy-колбэки ``on_delta``/``announce``
    сохраняют прежнее поведение CLI.
    """

    def emit(event: DebateEvent) -> None:
        """Единая точка публикации событий: sink + конвертация в legacy-колбэки."""
        if sink is not None:
            sink(event)
        if announce is not None and event.type == EVENT_AGENT_START:
            announce(event.speaker_title or SPEAKER_TITLES.get(event.role, ""), event.round or 0)
        if on_delta is not None and event.type == EVENT_DELTA:
            on_delta(event.text or "")

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

    def claimant_turn(state: DebateState) -> dict:
        """Реплика юриста заявителя."""
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
            )
        )
        logger.info("Узел %s: реплика %d симв. (раунд %d).", NODE_CLAIMANT, len(statement.text), round_number)
        return {"history": [statement], "round_number": round_number}

    def defendant_turn(state: DebateState) -> dict:
        """Реплика юриста ответчика."""
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
            )
        )
        logger.info("Узел %s: реплика %d симв. (раунд %d).", NODE_DEFENDANT, len(statement.text), round_number)
        return {"history": [statement]}

    def judge_review(state: DebateState) -> dict:
        """Оценка судьи по итогам раунда + решение о ходе процесса."""
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
        """Условный переход после судьи: новый раунд или итоговое решение."""
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
        """Итоговое мотивированное решение судьи."""
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
            )
        )
        emit(
            DebateEvent(
                EVENT_VERDICT_DONE,
                role=ROLE_JUDGE,
                round=round_number,
                payload={"length": len(verdict)},
            )
        )
        logger.info("Узел %s: решение готово (%d симв.).", NODE_VERDICT, len(verdict))
        return {"verdict": verdict}

    graph = StateGraph(DebateState)
    graph.add_node(NODE_INIT, init)
    graph.add_node(NODE_CLAIMANT, claimant_turn)
    graph.add_node(NODE_DEFENDANT, defendant_turn)
    graph.add_node(NODE_JUDGE, judge_review)
    graph.add_node(NODE_VERDICT, final_verdict)

    graph.add_edge(START, NODE_INIT)
    graph.add_edge(NODE_INIT, NODE_CLAIMANT)
    graph.add_edge(NODE_CLAIMANT, NODE_DEFENDANT)
    graph.add_edge(NODE_DEFENDANT, NODE_JUDGE)
    graph.add_conditional_edges(
        NODE_JUDGE,
        should_continue,
        {NODE_CLAIMANT: NODE_CLAIMANT, NODE_VERDICT: NODE_VERDICT},
    )
    graph.add_edge(NODE_VERDICT, END)

    logger.info("Граф прений собран: init -> claimant_turn -> defendant_turn -> judge_review -> ...")
    return graph.compile()


#: Кэш материалов дела в пределах процесса (ключ — корень проекта).
_case_cache: dict[str, CaseMaterials] = {}


def _load_case_cached(cfg: Config) -> CaseMaterials:
    """Материалы дела с кэшем: за прогон симуляции дело загружается один раз."""
    key = str(cfg.project_root)
    if key not in _case_cache:
        _case_cache[key] = load_case(cfg)
    return _case_cache[key]


def clear_case_cache() -> None:
    """Сбросить кэш материалов дела (например, если файлы дела изменились)."""
    _case_cache.clear()


def run_debate(
    *,
    max_rounds: int | None = None,
    sink: EventSink | None = None,
    on_delta: OutputCallback | None = None,
    announce: SpeakerAnnouncer | None = None,
    config_path: str | None = None,
) -> DebateResult:
    """Запустить симуляцию прений и вернуть итог (история + вердикт).

    :param max_rounds: переопределение лимита раундов из конфига (для быстрых прогонов).
    :param sink: подписчик на события :class:`DebateEvent` (консоль/WebSocket).
    :param on_delta: legacy-колбэк стриминга текста агентов (вывод в консоль).
    :param announce: legacy-колбэк объявления спикера (название роли, номер раунда).
    :param config_path: путь к config.yaml (по умолчанию — config.yaml проекта).
    """
    cfg = load_config(config_path) if config_path else load_config()
    if max_rounds is not None:
        if max_rounds < 1:
            raise ValueError(f"max_rounds должен быть >= 1, получено: {max_rounds}.")
        cfg = replace(cfg, max_rounds=max_rounds)

    reset_clients()
    set_config(cfg)  # клиенты и суммаризатор должны использовать тот же конфиг
    clear_case_cache()
    logger.info(
        "Старт симуляции: юрисдикция=%s; модели: заявитель=%s, ответчик=%s, судья=%s; max_rounds=%d.",
        cfg.jurisdiction,
        cfg.model_claimant_lawyer,
        cfg.model_defendant_lawyer,
        cfg.model_judge,
        cfg.max_rounds,
    )

    graph = build_graph(cfg, sink=sink, on_delta=on_delta, announce=announce)
    final: DebateState = graph.invoke({})

    decision = final.get("judge_decision")
    result = DebateResult(
        cfg=cfg,
        materials=final["materials"],
        history=list(final.get("history", [])),
        verdict=final["verdict"],
        rounds_played=final["round_number"],
        finished_by_judge=bool(decision is not None and not decision.continues),
    )
    logger.info(
        "Симуляция завершена: раундов=%d, завершена судьёй=%s, реплик=%d, вердикт=%d симв.",
        result.rounds_played,
        result.finished_by_judge,
        len(result.history),
        len(result.verdict),
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
                },
            )
        )
    return result
