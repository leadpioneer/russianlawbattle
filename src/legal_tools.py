"""Поиск актуальных норм права через MCP-сервер pravo-mcp (pravo.gov.ru).

DEPRECATED (этап 3): это временный совместимый фасад над новым слоем
``src.legal`` (провайдеры ``src.legal.providers.*``, сервис и Evidence Pack).
Держится, чтобы граф и CLI работали без изменений; новые потребители должны
использовать ``src.legal``. Снос фасада — после переноса потребителей (шаг 6).

Обёртка ``search_law(query)`` для агентов:
  1) ``search_npa(query, limit)`` — кандидаты из официального портала правовой
     информации РФ (publication.pravo.gov.ru, без API-ключей);
  2) для лучшего совпадения — ``get_npa(eid)``: текст/метаданные акта.

Деградация (по ТЗ): сервер недоступен/пустой ответ → возвращается пустой список
и заполняется ``last_error``; агенты в этом случае обязаны помечать ссылки на
нормы как «требует проверки», а отчёт получает соответствующее предупреждение.
Результаты кэшируются в процессе на 10 минут (вопросы судьи и юристов часто
повторяются; сам сервер тоже кэширует на своей стороне).
"""

from __future__ import annotations

import asyncio
import html as html_module
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Команда запуска MCP-сервера pravo (stdio-транспорт, тот же интерпретатор).
DEFAULT_COMMAND: tuple[str, ...] = (sys.executable, "-X", "utf8", "-m", "pravo_mcp.server")

#: Таймаут одного MCP-вызова, сек (сервер сам ограничивает HTTP-запрос 60 с).
CALL_TIMEOUT_S = 90.0

#: Таймаут всей операции search_law (коннект + поиск + get_npa), сек.
SEARCH_TIMEOUT_S = 120.0

#: TTL кэша обёртки, сек (совпадает с кэшем сервера).
_CACHE_TTL_S = 600.0


@dataclass(frozen=True)
class LawExcerpt:
    """Норма права, подтверждённая внешним источником (pravo.gov.ru)."""

    eid: str  # идентификатор документа на портале
    title: str  # название акта
    act_type: str  # тип документа (federal_law и т.п.), если известен
    number: str  # номер акта, если известен
    date: str  # дата акта, если известна
    source_url: str  # ссылка на официальную публикацию
    text: str  # текст (или очищенный фрагмент); пуст, если доступен только PDF/XML
    text_available: bool  # True — inline-текст получен; False — только pdfUrl/xmlUrl
    verified: bool  # True — данные получены из pravo.gov.ru через MCP

    def context_block(self) -> str:
        """Компактный блок для подстановки в промпт агента."""
        head = f"{self.title}"
        if self.number:
            head += f" № {self.number}"
        if self.date:
            head += f" от {self.date}"
        lines = [f"- {head} [{self.source_url}]"]
        if self.text:
            lines.append(f"  Фрагмент: {self.text[:600]}")
        else:
            lines.append(
                "  (текст не загружен; реквизиты акта подтверждены публикацией на "
                "pravo.gov.ru — сверяйте текст нормы по ссылке)"
            )
        return "\n".join(lines)


#: Последняя ошибка MCP (для диагностики и предупреждений в отчёте).
last_error: str | None = None

_cache: dict[str, tuple[float, list[LawExcerpt]]] = {}
_cache_lock = threading.Lock()

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    """Грубая очистка HTML в читаемый текст (для MVP достаточно)."""
    text = _TAG_RE.sub(" ", html)
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _unwrap_result(result: Any) -> Any:
    """Достать полезную нагрузку из CallToolResult (mcp SDK / fastmcp)."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    raise RuntimeError("MCP-инструмент вернул пустой ответ")


def _normalize_hits(payload: Any) -> list[dict[str, Any]]:
    """Привести ответ search_npa к списку словарей."""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("result") or payload.get("documents") or []
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def _excerpt_from(item: dict[str, Any], detail: dict[str, Any] | None) -> LawExcerpt:
    """LawExcerpt из записи поиска + (опционально) данных get_npa."""
    merged: dict[str, Any] = {**item, **{k: v for k, v in (detail or {}).items() if v}}
    eid = str(merged.get("eid") or merged.get("id") or "")
    html_content = merged.get("htmlContent") or merged.get("content") or ""
    text = _strip_html(html_content) if isinstance(html_content, str) and html_content else ""
    # complexName — полное название акта (у name бывают обрезанные первые строки).
    raw_title = str(merged.get("complexName") or merged.get("name") or "Без названия")
    title = _WS_RE.sub(" ", raw_title).strip()
    return LawExcerpt(
        eid=eid,
        title=title,
        act_type=str(merged.get("documentType") or merged.get("type") or ""),
        number=str(merged.get("number") or ""),
        date=str(merged.get("documentDate") or merged.get("date") or ""),
        source_url=str(
            merged.get("publicationUrl")
            or (f"https://publication.pravo.gov.ru/Document/View/{eid}" if eid else "")
        ),
        text=text,
        text_available=bool(text),
        verified=True,
    )


#: Номер федерального акта в тексте запроса: «152-ФЗ», «402-ФЗО», «1-ФКЗ».
_ACT_NUMBER_RE = re.compile(r"\b\d{1,4}-(?:ФЗО|ФКЗ|ФЗ)\b", re.IGNORECASE)

#: Маркеры региональных/ведомственных актов — понижают ранг (нам нужны федеральные нормы).
_REGION_RE = re.compile(
    r"области|республик|автономн|края|краев|городе москве|московской|петербург|муниципальн",
    re.IGNORECASE,
)


def _prepare_query(query: str) -> tuple[str, str]:
    """Если в запросе есть номер акта — искать по нему (надёжнее), иначе как есть.

    :returns: (поисковая строка, найденный номер акта или "").
    """
    match = _ACT_NUMBER_RE.search(query)
    if match:
        number = match.group(0).upper()
        return number, number
    return query.strip(), ""


def _rank_hits(
    items: list[dict[str, Any]], original_query: str, act_number: str
) -> list[dict[str, Any]]:
    """Ранжирование шума подстрочного поиска: федеральные акты с релевантным именем — выше."""
    query_lower = original_query.lower().strip()

    def score(item: dict[str, Any]) -> int:
        name = str(item.get("name") or "")
        complex_name = str(item.get("complexName") or "")
        combined = f"{name} {complex_name}".lower()
        value = 0
        if act_number and act_number.lower() in combined:
            value += 4
        if query_lower and query_lower in combined:
            value += 3
        if "федерального закона" in combined or "закона российской федерации" in combined:
            value += 2  # федеральные нормы важнее региональных/ведомственных однофамильцев
        name_lower = name.lower()
        if "внесении измен" in name_lower or "внесении дополнен" in name_lower:
            value -= 2
        if _REGION_RE.search(name):
            value -= 2
        if name_lower.startswith(("о ", "об ", '"о ', "федеральный закон")):
            value += 1
        return value

    return sorted(items, key=score, reverse=True)


async def _search_law_async(query: str, *, limit: int, command: tuple[str, ...]) -> list[LawExcerpt]:
    """Живой обход MCP: initialize → list_tools → search_npa → get_npa (топ-1)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=command[0], args=list(command[1:]))
    search_query, act_number = _prepare_query(query)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), CALL_TIMEOUT_S)
            tools = await asyncio.wait_for(session.list_tools(), CALL_TIMEOUT_S)
            names = {tool.name for tool in tools.tools}
            if "search_npa" not in names:
                raise RuntimeError(f"pravo-mcp без search_npa (доступны: {sorted(names)})")

            response = await asyncio.wait_for(
                # ВАЖНО: doc_type не передаём — API портала отклоняет фильтр
                # (HTTP 400), шум убираем пост-ранжированием _rank_hits.
                session.call_tool("search_npa", {"query": search_query, "limit": limit}),
                CALL_TIMEOUT_S,
            )
            items = _rank_hits(_normalize_hits(_unwrap_result(response)), query, act_number)
            if not items:
                return []

            excerpts: list[LawExcerpt] = []
            for rank, item in enumerate(items):
                detail: dict[str, Any] | None = None
                eid = item.get("eid") or item.get("id")
                if rank == 0 and eid and "get_npa" in names:
                    try:
                        detail_response = await asyncio.wait_for(
                            session.call_tool("get_npa", {"eid": eid}),
                            CALL_TIMEOUT_S,
                        )
                        unwrapped = _unwrap_result(detail_response)
                        detail = unwrapped if isinstance(unwrapped, dict) else None
                    except Exception as exc:  # noqa: BLE001 — текст может не догрузиться
                        logger.warning("get_npa(%s) не удался: %s", eid, exc)
                excerpts.append(_excerpt_from(item, detail))
            return excerpts


def search_law(
    query: str,
    jurisdiction: str = "",
    *,
    limit: int = 3,
    legal_mcp: dict[str, Any] | None = None,
    use_cache: bool = True,
) -> list[LawExcerpt]:
    """Найти нормы права по запросу; [] при любой ошибке (см. ``last_error``).

    :param query: поисковый запрос (тема/норма), например
        «потребитель вправе вернуть товар при недостатках».
    :param jurisdiction: юрисдикция из конфига; pravo-mcp покрывает только РФ —
        для остальных юрисдикций поиск не выполняется.
    :param limit: сколько кандидатов запрашивать (топ-1 разворачивается в текст).
    :param legal_mcp: секция ``legal_mcp`` из конфига: ``enabled``, ``command``, ``limit``.
    :param use_cache: использовать кэш обёртки (10 минут).
    """
    global last_error

    options = legal_mcp or {}
    if options.get("enabled") is False:
        return []
    if jurisdiction and not any(  # MCP-источник покрывает только федеральное право РФ
        token in jurisdiction.lower() for token in ("рф", "росси", "russia")
    ):
        return []

    query = (query or "").strip()
    if not query:
        return []
    limit = int(options.get("limit") or limit)

    cache_key = f"{query.lower()}|{limit}"
    with _cache_lock:
        cached = _cache.get(cache_key)
        if use_cache and cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_S:
            return cached[1]

    raw_command = options.get("command") or list(DEFAULT_COMMAND)
    command = tuple(str(part) for part in raw_command)

    try:
        excerpts = asyncio.run(
            asyncio.wait_for(
                _search_law_async(query, limit=limit, command=command),
                SEARCH_TIMEOUT_S,
            )
        )
    except Exception as exc:  # noqa: BLE001 — деградация вместо падения симуляции
        last_error = f"{type(exc).__name__}: {exc}"
        logger.warning("search_law(%r) не удался: %s", query, last_error)
        return []

    last_error = None
    with _cache_lock:
        _cache[cache_key] = (time.monotonic(), excerpts)
    logger.info(
        "search_law(%r): найдено %d норм (топ: %s).",
        query,
        len(excerpts),
        excerpts[0].title if excerpts else "—",
    )
    return excerpts


def clear_cache() -> None:
    """Сбросить кэш обёртки (для тестов)."""
    with _cache_lock:
        _cache.clear()
