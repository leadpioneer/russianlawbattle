"""Агент «юрист заявителя»: системный промпт и генерация реплики по раундам."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ..config import Config
from ..document_loader import CaseMaterials
from ..llm_client import DeltaCallback, chat
from .base import ROLE_CLAIMANT, Statement, case_block, jurisdiction_guidance, render_history

logger = logging.getLogger(__name__)

_ROLE_TITLE = "юрист заявителя (истца)"

_ROUND1_TASK = (
    "Раунд {round} прений. Изложи позицию заявителя по делу: требования, ключевые факты "
    "и правовое обоснование. Структурируй ответ тезисами, объём 250-450 слов."
)
_NEXT_ROUND_TASK = (
    "Раунд {round} прений. Ответь на аргументы юриста ответчика: возрази по существу и "
    "усиль позицию заявителя доводами из материалов дела. Объём 250-450 слов."
)


def build_system_prompt(cfg: Config, materials: CaseMaterials) -> str:
    """Системный промпт юриста заявителя (роль + юрисдикция + материалы дела)."""
    return "\n\n".join(
        [
            f"Ты — {_ROLE_TITLE} в судебном процессе, {cfg.jurisdiction}. Ты последовательно "
            "защищаешь интересы заявителя: убеждённо, но корректно, строго в рамках профессиональной этики.",
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
    norms_block: str = "",
    on_delta: DeltaCallback | None = None,
) -> Statement:
    """Реплика юриста заявителя для раунда ``round_number`` с учётом истории прений."""
    system_prompt = build_system_prompt(cfg, materials)
    if norms_block:
        system_prompt += f"\n\n{norms_block}"
    if round_number <= 1 or not history:
        task = _ROUND1_TASK.format(round=round_number)
    else:
        task = _NEXT_ROUND_TASK.format(round=round_number)
    user_prompt = (
        f"{task}\n\nИСТОРИЯ ПРЕНИЙ:\n"
        f"{render_history(history) if history else '(вы открываете прения)'}"
    )
    logger.info("Агент «%s» готовит реплику (раунд %d).", ROLE_CLAIMANT, round_number)
    text = chat(
        ROLE_CLAIMANT,
        system_prompt,
        [{"role": "user", "content": user_prompt}],
        temperature=0.7,
        on_delta=on_delta,
    )
    return Statement(speaker=ROLE_CLAIMANT, round=round_number, text=text)
