"""Агент «судья»: оценка раундов прений и итоговое мотивированное решение.

Судья — не спорящая сторона: после каждого раунда он оценивает реплики юристов
и решает, продолжать ли прения (с уточняющим вопросом стороне/сторонам) или
завершать процесс и выносить решение. Решение кодируется строкой-маркером в
конце ответа (см. ``JUDGE_MARKER_INSTRUCTION``), которую разбирает граф.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..config import Config
from ..document_loader import CaseMaterials
from ..llm_client import DeltaCallback, chat
from .base import (
    ROLE_CLAIMANT,
    ROLE_DEFENDANT,
    ROLE_JUDGE,
    Statement,
    case_block,
    jurisdiction_guidance,
    render_history,
)

logger = logging.getLogger(__name__)

#: Маркер решения судьи в конце ответа (парсится графом).
_MARKER_RE = re.compile(
    r"===\s*РЕШЕНИЕ\s+СУДЬИ:\s*(ПРОДОЛЖАТЬ|ЗАВЕРШИТЬ)"
    r"(?:\s*\(кому:\s*(claimant_lawyer|defendant_lawyer|both)\s*\))?\s*===",
    re.IGNORECASE,
)

_JUDGE_MARKER_INSTRUCTION = """\
В самом конце ответа ОБЯЗАТЕЛЬНО добавь ровно одну строку-маркер, в точности один из вариантов:
=== РЕШЕНИЕ СУДЬИ: ПРОДОЛЖАТЬ (кому: claimant_lawyer) ===
=== РЕШЕНИЕ СУДЬИ: ПРОДОЛЖАТЬ (кому: defendant_lawyer) ===
=== РЕШЕНИЕ СУДЬИ: ПРОДОЛЖАТЬ (кому: both) ===
=== РЕШЕНИЕ СУДЬИ: ЗАВЕРШИТЬ ===
ПРОДОЛЖАТЬ — если требуется уточнение или новые аргументы (укажи, кому адресован вопрос);
ЗАВЕРШИТЬ — если материалов и аргументов достаточно для решения."""


@dataclass(frozen=True)
class JudgeDecision:
    """Машиночитаемое решение судьи по итогам раунда."""

    continues: bool  # True — ещё раунд; False — переход к итоговому решению
    addressee: str  # кому вопрос: claimant_lawyer / defendant_lawyer / both ("" если завершение)
    question: str  # текст судьи без строки-маркера


def build_review_prompt(cfg: Config, materials: CaseMaterials) -> str:
    """Системный промпт судьи для проверки раунда прений."""
    return "\n\n".join(
        [
            f"Ты — судья в судебном процессе, {cfg.jurisdiction}. Ты не участвуешь в споре "
            "и не защищаешь ни одну из сторон: ты оцениваешь аргументы, следишь за существом "
            "дела и управляешь ходом прений.",
            jurisdiction_guidance(cfg.jurisdiction),
            case_block(materials),
            "По итогам каждого раунда: (1) кратко оцени аргументы сторон — что подтверждается "
            "материалами, что спорно, что не раскрыто; (2) реши, продолжать ли прения: если "
            "есть неясности — задай ОДИН конкретный уточняющий вопрос стороне/сторонам; если "
            "доводы сторон по существу исчерпаны — завершай процесс.\n" + _JUDGE_MARKER_INSTRUCTION,
        ]
    )


def parse_judge_decision(text: str) -> JudgeDecision:
    """Извлечь решение судьи из строки-маркера; без маркера — «продолжать, вопрос обеим»."""
    match = _MARKER_RE.search(text)
    if match is None:
        logger.warning("Судья не оставил маркер решения — трактуем как ПРОДОЛЖАТЬ (both).")
        return JudgeDecision(continues=True, addressee="both", question=text.strip())
    verb = match.group(1).upper()
    addressee = (match.group(2) or "").lower()
    question = text[: match.start()].strip()
    decision = JudgeDecision(
        continues=(verb == "ПРОДОЛЖАТЬ"),
        addressee=addressee if verb == "ПРОДОЛЖАТЬ" else "",
        question=question,
    )
    logger.info(
        "Решение судьи: %s (кому: %s).",
        "ПРОДОЛЖАТЬ" if decision.continues else "ЗАВЕРШИТЬ",
        decision.addressee or "—",
    )
    return decision


def review_round(
    cfg: Config,
    materials: CaseMaterials,
    round_number: int,
    history: Sequence[Statement],
    *,
    on_delta: DeltaCallback | None = None,
) -> tuple[Statement, JudgeDecision]:
    """Оценка судьи по итогам раунда: текст-реплика + машиночитаемое решение."""
    user_prompt = (
        f"Раунд {round_number} прений завершён. ИСТОРИЯ ПРЕНИЙ:\n\n{render_history(history)}\n\n"
        "Дай краткую оценку аргументов сторон и прими решение о ходе процесса "
        "(не забудьте строку-маркер в конце)."
    )
    text = chat(
        ROLE_JUDGE,
        build_review_prompt(cfg, materials),
        [{"role": "user", "content": user_prompt}],
        temperature=0.2,
        on_delta=on_delta,
    )
    decision = parse_judge_decision(text)
    return Statement(speaker=ROLE_JUDGE, round=round_number, text=text), decision


def build_verdict_prompt(cfg: Config, materials: CaseMaterials) -> str:
    """Системный промпт судьи для итогового мотивированного решения."""
    return "\n\n".join(
        [
            f"Ты — судья в судебном процессе, {cfg.jurisdiction}. Прения завершены; "
            "ты выносишь итоговое мотивированное решение по существу спора.",
            jurisdiction_guidance(cfg.jurisdiction),
            case_block(materials),
            "Структура решения: 1) установленные обстоятельства дела; 2) позиции сторон и их "
            "оценка; 3) правовое обоснование — только реальные нормы, применимые к фактам "
            "дела; 4) резолютивная часть (что решил: удовлетворить/отказать/частично и в каком "
            "объёме); 5) распределение судебных расходов. Объём 600-1000 слов, деловой стиль.",
        ]
    )


def generate_verdict(
    cfg: Config,
    materials: CaseMaterials,
    history: Sequence[Statement],
    *,
    on_delta: DeltaCallback | None = None,
) -> str:
    """Итоговое мотивированное решение судьи по всей истории прений."""
    user_prompt = (
        f"Все раунды прений завершены (максимум достигнут или вы приняли решение завершить).\n"
        f"ПОЛНАЯ ИСТОРИЯ ПРЕНИЙ:\n\n{render_history(history)}\n\n"
        "Вынесите итоговое мотивированное решение по структуре из инструкции."
    )
    logger.info("Судья выносит итоговое решение по %d репликам.", len(history))
    return chat(
        ROLE_JUDGE,
        build_verdict_prompt(cfg, materials),
        [{"role": "user", "content": user_prompt}],
        temperature=0.2,
        on_delta=on_delta,
    )
