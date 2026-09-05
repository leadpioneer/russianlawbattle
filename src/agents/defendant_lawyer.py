"""Агент «юрист ответчика»: системный промпт и генерация реплики по раундам."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ..config import Config
from ..document_loader import CaseMaterials
from ..llm_client import DeltaCallback, chat
from .base import ROLE_DEFENDANT, Statement, case_block, jurisdiction_guidance, render_history

logger = logging.getLogger(__name__)

_ROLE_TITLE = "юрист ответчика"

_ROUND1_TASK = (
    "Раунд {round} прений. Изложи позицию ответчика по делу: возражения против требований "
    "заявителя, ключевые факты и правовое обоснование. Тезисы, объём 250-450 слов."
)
_NEXT_ROUND_TASK = (
    "Раунд {round} прений. Ответь на аргументы юриста заявителя: опровергни его доводы "
    "по существу и приведи контраргументы из материалов дела. Объём 250-450 слов."
)


def build_system_prompt(cfg: Config, materials: CaseMaterials) -> str:
    """Системный промпт юриста ответчика (роль + юрисдикция + материалы дела)."""
    return "\n\n".join(
        [
            f"Ты — {_ROLE_TITLE} в судебном процессе, {cfg.jurisdiction}. Ты последовательно "
            "защищаешь интересы ответчика: оспариваешь требования и доводы заявителя, убеждённо, "
            "но корректно, строго в рамках профессиональной этики.",
            jurisdiction_guidance(cfg.jurisdiction),
            case_block(materials),
            "Формат реплики: деловой русский язык, тезисы; факты — со ссылками на источники "
            "в квадратных скобках, правовые доводы — со ссылками на нормы. Без выдуманных фактов.",
        ]
    )


def make_statement(
    cfg: Config,
    materials: CaseMaterials,
    round_number: int,
    history: Sequence[Statement],
    *,
    on_delta: DeltaCallback | None = None,
) -> Statement:
    """Реплика юриста ответчика для раунда ``round_number`` с учётом истории прений."""
    system_prompt = build_system_prompt(cfg, materials)
    if round_number <= 1 or not history:
        task = _ROUND1_TASK.format(round=round_number)
    else:
        task = _NEXT_ROUND_TASK.format(round=round_number)
    user_prompt = (
        f"{task}\n\nИСТОРИЯ ПРЕНИЙ:\n"
        f"{render_history(history) if history else '(история пока пуста)'}"
    )
    logger.info("Агент «%s» готовит реплику (раунд %d).", ROLE_DEFENDANT, round_number)
    text = chat(
        ROLE_DEFENDANT,
        system_prompt,
        [{"role": "user", "content": user_prompt}],
        temperature=0.7,
        on_delta=on_delta,
    )
    return Statement(speaker=ROLE_DEFENDANT, round=round_number, text=text)
