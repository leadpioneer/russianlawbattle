"""Промежуточный шаг поиска норм права для реплик агентов.

Перед генерацией реплики модель роли коротким вызовом формулирует 1-2 поисковых
запроса — НАЗВАНИЯ актов или НОМЕРА («О защите прав потребителей», «152-ФЗ»).
Запросы уходят в :func:`src.legal_tools.search_law` (MCP pravo-mcp →
pravo.gov.ru), найденные реквизиты собираются в блок «ПРОВЕРЕННЫЕ НОРМЫ ПРАВА»,
который добавляется в системный промпт агента.

Правила достоверности (внутри блока): ссылаться только на нормы из списка или
из материалов дела; тексты статей не загружаются — указывать реквизиты без
цитирования содержания; любую норму вне списка и материалов помечать
«(требует проверки)»; не выдумывать номера статей.

Деградация: если MCP недоступен/ничего не нашёл и это не осознанное отключение —
``VerifiedNorms.degraded=True``, узел добавляет предупреждение в состояние графа
(попадает в отчёт и JSON API). Симуляция при этом продолжается.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..config import Config
from ..document_loader import CaseMaterials
from ..legal_tools import LawExcerpt, last_error, search_law
from ..llm_client import chat

logger = logging.getLogger(__name__)

#: Максимум запросов за одну реплику (скорость!).
MAX_QUERIES = 2

#: Максимум норм в блоке за одну реплику.
MAX_NORMS = 4

_QUERY_LINE_RE = re.compile(r"^\s*(?:\d+[.)]\s*|[-*•]\s*)?(.+?)\s*$")


@dataclass(frozen=True)
class VerifiedNorms:
    """Результат промежуточного шага поиска норм для одной реплики."""

    queries: tuple[str, ...]  # сформулированные моделью запросы
    excerpts: tuple[LawExcerpt, ...]  # что реально нашлось (дедуп по eid)
    block: str  # готовый текст для системного промпта ("" — норм нет)
    degraded: bool  # True — хотели подтвердить нормы, но не смогли


def _parse_queries(text: str) -> list[str]:
    """Разобрать ответ модели в список запросов (по строке на запрос)."""
    queries: list[str] = []
    for line in text.splitlines():
        match = _QUERY_LINE_RE.match(line)
        if match is None:
            continue
        query = match.group(1).strip().strip('"«»').strip()
        if len(query) < 4 or query.lower().startswith(("запрос", "query", "пример")):
            continue
        queries.append(query)
        if len(queries) >= MAX_QUERIES:
            break
    return queries


def build_verified_norms(cfg: Config, materials: CaseMaterials, role: str, purpose: str) -> VerifiedNorms:
    """Сформулировать запросы (модель роли) и найти нормы через MCP.

    :param purpose: человеческое описание задачи реплики, попадает в запрос
        («позиция заявителя (раунд 1 прений)», «итоговое решение судьи», …).
    """
    legal_cfg = cfg.legal_mcp or {}
    if legal_cfg.get("enabled") is False:  # осознанное отключение — не деградация
        return VerifiedNorms((), (), "", False)
    if not any(token in cfg.jurisdiction.lower() for token in ("рф", "росси", "russia")):
        return VerifiedNorms((), (), "", False)  # MCP-источник покрывает только РФ

    digest = materials.full_context()[:1500]
    query_prompt = (
        f"Ты готовишь {purpose} в судебном процессе, {cfg.jurisdiction}.\n"
        "Сформулируй 1-2 ПОИСКОВЫХ ЗАПРОСА для нахождения релевантных федеральных НПА "
        "на официальном портале pravo.gov.ru. Формат запроса — НАЗВАНИЕ акта "
        "(напр. «О защите прав потребителей») или НОМЕР акта (напр. «152-ФЗ»). "
        "Никаких вопросов — только названия или номера.\n\n"
        f"О ДЕЛЕ (фрагмент):\n{digest}\n\n"
        "Ответ: по одному запросу на строку, без нумерации и пояснений."
    )
    try:
        raw = chat(
            role,
            "Ты — юридический помощник. Отвечай только списком поисковых запросов.",
            [{"role": "user", "content": query_prompt}],
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001 — деградация вместо падения симуляции
        logger.warning("Формулирование запросов норм не удалось: %s", exc)
        return VerifiedNorms((), (), "", True)

    queries = _parse_queries(raw)
    if not queries:
        logger.warning("Модель не вернула поисковых запросов (%r).", raw[:120])
        return VerifiedNorms((), (), "", True)

    excerpts: list[LawExcerpt] = []
    seen: set[str] = set()
    mcp_failed = False
    for query in queries:
        found = search_law(query, cfg.jurisdiction, legal_mcp=legal_cfg)
        if not found:
            if last_error:
                mcp_failed = True
                logger.warning("Поиск норм %r не удался: %s", query, last_error)
            continue
        for excerpt in found:
            if excerpt.eid in seen:
                continue
            seen.add(excerpt.eid)
            excerpts.append(excerpt)

    excerpts = excerpts[:MAX_NORMS]
    if not excerpts:
        return VerifiedNorms(tuple(queries), (), "", mcp_failed)

    lines = ["ПРОВЕРЕННЫЕ НОРМЫ ПРАВА (реквизиты подтверждены порталом pravo.gov.ru):"]
    lines += [f"- {excerpt.title} [{excerpt.source_url}]" for excerpt in excerpts]
    lines += [
        "ПРАВИЛА ДОСТОВЕРНОСТИ: ссылайся на эти акты и на нормы из материалов дела; "
        "тексты статей не загружены — указывай только реквизиты актов, без цитирования "
        "содержания статей; любую норму вне этого списка и материалов дела помечай "
        "«(требует проверки)»; не выдумывай номера статей.",
    ]
    block = "\n".join(lines)
    logger.info(
        "Нормы для %s: запросы=%s, найдено=%d.",
        role,
        list(queries),
        len(excerpts),
    )
    return VerifiedNorms(tuple(queries), tuple(excerpts), block, False)
