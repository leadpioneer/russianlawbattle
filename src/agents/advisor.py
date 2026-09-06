"""Агент-аналитик: рекомендации для выбранной стороны по итогам прений.

Не участвует в споре. После вердикта судьи анализирует историю прений и решение
и готовит практический блок рекомендаций для стороны, выбранной пользователем
(``target_side``): сильные и слабые места позиции, риски в суде, конкретные
правки текста ответа на претензию и качественную оценку перспектив.

Модель — роль судьи (нейтральная и наиболее «сдержанная»), но с отдельным
системным промптом. Вывод — markdown фиксированной структуры; качественная
оценка перспектив извлекается из строки-маркера ``ПРОГНОЗ:``.
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
    ROLE_JUDGE,
    Statement,
    case_block,
    jurisdiction_guidance,
    render_history,
)

logger = logging.getLogger(__name__)

#: Название спикера для событий и консоли.
ADVISOR_TITLE = "Аналитик рекомендаций"

#: Валидные значения target_side.
TARGET_SIDES = ("claimant", "defendant")

#: Человекочитаемые названия сторон.
SIDE_TITLES = {"claimant": "заявителя", "defendant": "ответчика"}

_PROSPECTS_RE = re.compile(r"ПРОГНОЗ:\s*(высокие|средние|низкие)", re.IGNORECASE)

_STRUCTURE_INSTRUCTION = """Структура ответа — строго такие разделы (markdown):
## Сильные стороны позиции
(тезисы: что уже играет на пользу стороне, с опорой на материалы и прения)
## Слабые места и контраргументы
(что оспоримо, где позиция уязвима, чего не хватает в доказательствах)
## Риски при рассмотрении дела в суде
(что может сыграть против: процессуальные и материальные риски, поведение сторон)
## Как усилить текст ответа на претензию
(конкретные правки: что добавить, что убрать, какие доказательства приложить,
на какие нормы и факты опереться)
## Перспективы позиции
Первая строка раздела — ровно одна строка-маркер: «ПРОГНОЗ: высокие» или
«ПРОГНОЗ: средние» или «ПРОГНОЗ: низкие»; далее — обоснование оценки в 2-4 предложениях."""


@dataclass(frozen=True)
class Recommendation:
    """Блок рекомендаций для выбранной стороны."""

    target_side: str  # claimant | defendant
    text: str  # полный markdown-текст блока
    prospects: str  # качественная оценка: высокие / средние / низкие / неизвестно

    @property
    def side_title(self) -> str:
        """Название стороны для отчёта и веб-интерфейса."""
        return SIDE_TITLES.get(self.target_side, self.target_side)


def parse_prospects(text: str) -> str:
    """Извлечь качественную оценку из строки-маркера «ПРОГНОЗ:»."""
    match = _PROSPECTS_RE.search(text)
    if match is None:
        logger.warning("Аналитик не оставил маркер ПРОГНОЗ — оценка «неизвестно».")
        return "неизвестно"
    return match.group(1).lower()


def build_system_prompt(cfg: Config, materials: CaseMaterials, target_side: str) -> str:
    """Системный промпт аналитика: роль, юрисдикция, материалы, структура."""
    side_desc = (
        "заявителя (истца) — того, кто предъявил требования"
        if target_side == "claimant"
        else "ответчика — того, к кому предъявлены требования"
    )
    return "\n\n".join(
        [
            f"Ты — опытный юрист-аналитик, {cfg.jurisdiction}. Прения сторон завершены, судья "
            f"вынес решение. Ты готовишь практический блок рекомендаций для стороны {side_desc}: "
            "цель — помочь подготовиться к реальному судебному спору на этапе досудебной претензии. "
            "Ты не участник спора: твоя задача — трезвый разбор, а не адвокирование.",
            jurisdiction_guidance(cfg.jurisdiction),
            case_block(materials),
            "Будь конкретен и честен: не приукрашивай перспективы, прямо называй слабые места. "
            "Опирайся только на материалы дела, реплики прений и решение судьи. Нормы права "
            "упоминай те, что фигурировали в деле или решении; если ссылаешься на норму, которой "
            "нет в материалах, — добавляй пометку «(требует проверки)».",
            _STRUCTURE_INSTRUCTION,
        ]
    )


def generate_recommendations(
    cfg: Config,
    materials: CaseMaterials,
    history: Sequence[Statement],
    verdict: str,
    target_side: str,
    *,
    on_delta: DeltaCallback | None = None,
) -> Recommendation:
    """Сформировать блок рекомендаций для стороны ``target_side`` по итогам прений."""
    if target_side not in TARGET_SIDES:
        raise ValueError(f"target_side должен быть одним из {TARGET_SIDES}, получено: {target_side!r}.")

    user_prompt = (
        "ИСТОРИЯ ПРЕНИЙ:\n\n"
        f"{render_history(history)}\n\n"
        "ИТОГОВОЕ РЕШЕНИЕ СУДЬИ:\n\n"
        f"{verdict.strip()}\n\n"
        f"Подготовь блок рекомендаций для стороны «{SIDE_TITLES[target_side]}» "
        "строго по структуре из инструкции."
    )
    logger.info("Агент «%s» готовит рекомендации (target_side=%s).", ADVISOR_TITLE, target_side)
    text = chat(
        ROLE_JUDGE,
        build_system_prompt(cfg, materials, target_side),
        [{"role": "user", "content": user_prompt}],
        temperature=0.3,
        on_delta=on_delta,
    )
    prospects = parse_prospects(text)
    logger.info("Рекомендации готовы (%d симв.), перспектива: %s.", len(text), prospects)
    return Recommendation(target_side=target_side, text=text, prospects=prospects)
