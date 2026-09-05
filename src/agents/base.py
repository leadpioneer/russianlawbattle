"""Общие элементы агентов: реплики, история прений, юрисдикция, материалы дела."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..config import Config
from ..document_loader import CaseMaterials

logger = logging.getLogger(__name__)

#: Канонические роли (совпадают с ключами ролей в config.py).
ROLE_CLAIMANT = "claimant_lawyer"
ROLE_DEFENDANT = "defendant_lawyer"
ROLE_JUDGE = "judge"

#: Человекочитаемые названия ролей для истории и отчёта.
SPEAKER_TITLES: dict[str, str] = {
    ROLE_CLAIMANT: "Юрист заявителя",
    ROLE_DEFENDANT: "Юрист ответчика",
    ROLE_JUDGE: "Судья",
}


@dataclass(frozen=True)
class Statement:
    """Реплика участника процесса с привязкой к раунду."""

    speaker: str  # ROLE_CLAIMANT / ROLE_DEFENDANT / ROLE_JUDGE
    round: int  # номер раунда прений (с 1)
    text: str  # полный текст реплики

    @property
    def speaker_title(self) -> str:
        """Название роли для истории и отчёта."""
        return SPEAKER_TITLES.get(self.speaker, self.speaker)


def render_history(history: Iterable[Statement]) -> str:
    """История прений по раундам — для подстановки в промпты и отчёт."""
    lines: list[str] = []
    current_round: int | None = None
    for statement in history:
        if statement.round != current_round:
            current_round = statement.round
            lines.append(f"\n=== РАУНД {statement.round} ===")
        lines.append(f"[{statement.speaker_title}]:\n{statement.text.strip()}")
    return "\n\n".join(lines).strip()


def jurisdiction_guidance(jurisdiction: str) -> str:
    """Инструкция по юрисдикции для системных промптов агентов."""
    low = jurisdiction.lower()
    if "рф" in low or "российск" in low or "россия" in low:
        return (
            f"Юрисдикция: {jurisdiction}. Применяй материальное право РФ (прежде всего ГК РФ; "
            "в потребительских спорах — Закон РФ «О защите прав потребителей», в трудовых — ТК РФ и т.п.). "
            "Процессуальные ориентиры: ГПК РФ — для судов общей юрисдикции, АПК РФ — для арбитражных судов. "
            "Ссылайся на конкретные статьи только если они приведены в материалах дела или являются "
            "общеизвестными нормами; НИКОГДА не выдумывай номера статей и содержание законов."
        )
    return (
        f"Юрисдикция: {jurisdiction}. Опирайся на действующее право указанной юрисдикции; "
        "ссылайся на нормы только из материалов дела или общеизвестные; не выдумывай реквизиты актов."
    )


def case_block(materials: CaseMaterials) -> str:
    """Блок материалов дела для системного промпта с запретом на выдумывание фактов."""
    return (
        "МАТЕРИАЛЫ ДЕЛА (единственный источник фактов):\n"
        f"{materials.full_context()}\n\n"
        "Используй ТОЛЬКО факты из этих материалов. Не выдумывай обстоятельства, даты, "
        "суммы и документы, которых там нет. При ссылке на факт указывай источник в "
        "квадратных скобках, например [case_files/dogovor.docx]."
    )
