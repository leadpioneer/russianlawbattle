"""Провайдер официального портала pravo.gov.ru (шаги 3–4 этапа 3).

Проверенный живой контракт (06.09.2026):
- поиск: ``GET {base}/api/Documents?name=<название>&PageSize=10&Page=1``
  (PageSize обязателен; значения не кратные 10 дают HTTP 400; doc_type фильтр
  API отклоняет); ответ ``{"items": [...], "itemsTotalCount": …}``;
- у каждого item: ``eoNumber`` (номер опубликования ``0001202607260025``),
  ``name``, ``complexName``, ``number``, ``documentDate``, ``pdfFileLength``;
- файл: ``GET {base}/file/pdf?eoNumber=<eoNumber>`` → PDF (официальный скан,
  БЕЗ текстового слоя — извлекать текст из него нельзя, только хранить);
- страница документа: ``GET {base}/document/<eoNumber>`` (HTML, 200).

Текст статьи (машиночитаемый) портал не отдаёт: используем markitdown-слой
(:mod:`src.legal.converters`), который забирает текст с КонсультантПлюс и
честно помечает источник. Никогда не выдаём текст Консультанта за
«подтверждённый порталом» — только реквизиты подтверждены pravo.gov.ru.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from ..models import LegalSource, ProviderHealth, now_iso
from ..converters import find_consultant_article

_ACT_NUMBER_RE = re.compile(r"\b\d{1,4}-(?:ФЗО|ФКЗ|ФЗ)\b", re.IGNORECASE)
_REGION_RE = re.compile(
    r"области|республик|автономн|края|краев|городе москве|московской|петербург|муниципальн",
    re.IGNORECASE,
)

#: «статья 309» / «ст. 6.1.1» — номер статьи в запросе.
_ARTICLE_NUM_RE = re.compile(r"ст(?:ать\w*|\.?)\s+(\d+(?:\.\d+)*)", re.IGNORECASE)
#: Разворот аббревиатур кодексов для поиска по названию акта на портале.
_EXPANSIONS: dict[str, str] = {
    "гк рф": "Гражданский кодекс",
    "тк рф": "Трудовой кодекс",
    "апк рф": "Арбитражный процессуальный кодекс",
    "гпк рф": "Гражданский процессуальный кодекс",
    "коап рф": "Кодекс об административных правонарушениях",
    "нк рф": "Налоговый кодекс",
    "ск рф": "Семейный кодекс",
    "зозпп": "защите прав потребителей",
}


def _expand_abbreviations(query: str) -> str:
    """«статья 309 ГК РФ» → «Гражданский кодекс» (если аббревиатура опознана)."""
    lowered = query.lower()
    for abbrev, full in _EXPANSIONS.items():
        if abbrev in lowered:
            return full
    return query


def _expand_act_name(query: str) -> str:
    """Резервный разворот: убрать слова статьи, оставить название акта."""
    cleaned = _ARTICLE_NUM_RE.sub("", query)
    cleaned = re.sub(r"\b(рф|российской федерации)\b", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ,.-") or query


#: База официального портала (HTTPS сломан upstream — работает только HTTP).
PRAVO_BASE_URL = "http://publication.pravo.gov.ru"

#: Таймаут HTTP-запросов к порталу, сек.
HTTP_TIMEOUT_S = 30.0

#: PageSize поиска (API принимает кратные 10).
PAGE_SIZE = 10


@dataclass(frozen=True)
class PravoHit:
    """Сырая запись поиска портала (до нормализации в LegalSource)."""

    eid: str
    eo_number: str
    name: str
    complex_name: str
    number: str
    document_date: str
    pdf_file_length: int | None


def search_documents(query: str, *, limit: int = 8) -> list[PravoHit]:
    """Синхронный поиск по официальному порталу (рёбра деградации — исключения).

    Портал ищет подстроку по названию акта, поэтому запрос «статья 309 ГК РФ»
    (в названиях актов короткие обозначения кодексов не встречаются) даёт 0
    результатов. Стратегия: запрос с номером акта — как есть; иначе — разворот
    аббревиатур («ГК РФ» → «Гражданский кодекс») и запрос по имени акта.
    """
    q = (query or "").strip()
    if not q:
        return []
    act_number = _ACT_NUMBER_RE.search(q)
    if act_number:  # по номеру акта ищем как есть — это надёжнее
        search_q = act_number.group(0).upper()
    else:
        search_q = _expand_abbreviations(q)
    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
        response = client.get(
            f"{PRAVO_BASE_URL}/api/Documents",
            params={"name": search_q, "PageSize": PAGE_SIZE, "Page": 1},
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items", []) if isinstance(payload, dict) else payload
        # Фолбэк: точная фраза не нашлась — пробуем развёрнутое имя акта.
        if not items and search_q != q:
            expanded = _EXPANSIONS.get(q.lower()) or _expand_act_name(q)
            if expanded and expanded.lower() != search_q.lower():
                response = client.get(
                    f"{PRAVO_BASE_URL}/api/Documents",
                    params={"name": expanded, "PageSize": PAGE_SIZE, "Page": 1},
                )
                response.raise_for_status()
                payload = response.json()
                items = payload.get("items", []) if isinstance(payload, dict) else payload
    items = payload.get("items", []) if isinstance(payload, dict) else payload
    hits = [
        PravoHit(
            eid=str(item.get("id") or ""),
            eo_number=str(item.get("eoNumber") or ""),
            name=str(item.get("name") or ""),
            complex_name=str(item.get("complexName") or ""),
            number=str(item.get("number") or ""),
            document_date=str(item.get("documentDate") or ""),
            pdf_file_length=item.get("pdfFileLength"),
        )
        for item in items
        if isinstance(item, dict) and item.get("id")
    ]
    return _rank(hits, q)[:limit]


def _rank(hits: list[PravoHit], original_query: str) -> list[PravoHit]:
    """Ранжирование шума подстрочного поиска (аналог legal_tools._rank_hits)."""
    query_lower = original_query.lower().strip()

    def score(hit: PravoHit) -> int:
        name = hit.name.lower()
        combined = f"{hit.name} {hit.complex_name}".lower()
        value = 0
        act_number = _ACT_NUMBER_RE.search(original_query)
        if act_number and act_number.group(0).lower() in combined:
            value += 4
        if query_lower and query_lower in combined:
            value += 3
        if "федерального закона" in combined or "закона российской федерации" in combined:
            value += 2
        if "внесении измен" in name or "внесении дополнен" in name:
            value -= 2
        if _REGION_RE.search(name):
            value -= 2
        if name.startswith(("о ", "об ", '"о ', "федеральный закон")):
            value += 1
        return value

    return sorted(hits, key=score, reverse=True)


def document_page_url(eo_number: str) -> str:
    """Страница официального опубликования: /document/<eoNumber>."""
    return f"{PRAVO_BASE_URL}/document/{eo_number}"


def pdf_url(eo_number: str) -> str:
    """Прямая ссылка на официальный PDF: /file/pdf?eoNumber=<eoNumber>."""
    return f"{PRAVO_BASE_URL}/file/pdf?eoNumber={eo_number}"


def hit_to_legal_source(hit: PravoHit, *, seq: int, prefix: str = "LAW") -> LegalSource:
    """Нормализация записи поиска в LegalSource.

    verified=False всегда: текст статьи портал не отдаёт, значит источник
    может быть максимум partially_verified (реквизиты подтверждены, текст —
    нет). excerpt остаётся пустым до успешного добора текста конвертером.
    """
    date = hit.document_date.split("T")[0] if hit.document_date else None
    title = re.sub(r"\s+", " ", hit.complex_name or hit.name).strip()
    return LegalSource(
        id=f"{prefix}-{seq:03d}",
        source_type="statute",
        title=title,
        authority="pravo.gov.ru",
        citation=title,
        excerpt="",  # заполняется converters при успешном доборе текста
        official_url=document_page_url(hit.eo_number) if hit.eo_number else None,
        effective_date=date,
        verified=False,
        verification_status="partially_verified",
        provider="pravo_gov",
        retrieved_at=now_iso(),
        relevance_score=0.0,
        warning=(
            "реквизиты подтверждены официальной публикацией pravo.gov.ru; "
            "текст нормы нужно сверять по ссылке"
        ),
    )


class PravoGovProvider:
    """Провайдер «официальные реквизиты + текст статьи из внешнего источника».

    Реализует LegalProvider (async): поиск реквизитов — только pravo.gov.ru;
    текст статьи добирается converters.find_consultant_article (markitdown),
    но статус остаётся partially_verified (текст неофициальный).
    search_case_law возвращает [] и честно не декларирует capability case_law.
    """

    name = "pravo_gov"

    def __init__(self, *, enrich_text: bool = True) -> None:
        self._enrich_text = enrich_text

    async def healthcheck(self) -> ProviderHealth:
        checked_at = now_iso()
        try:
            hits = await _to_thread(search_documents, "Гражданский кодекс", limit=1)
        except Exception as exc:  # noqa: BLE001 — healthcheck не бросает
            return ProviderHealth(
                provider=self.name,
                status="unavailable",
                transport="direct_api",
                checked_at=checked_at,
                capabilities=[],
                message=f"{type(exc).__name__}: {exc}",
            )
        if not hits:
            return ProviderHealth(
                provider=self.name,
                status="degraded",
                transport="direct_api",
                checked_at=checked_at,
                capabilities=["statutes"],
                message="поиск отвечает, но тестовый запрос не дал результатов",
            )
        return ProviderHealth(
            provider=self.name,
            status="healthy",
            transport="direct_api",
            checked_at=checked_at,
            capabilities=["statutes", "documents"],
            message=f"поиск ok (тестовых хитов: {len(hits)}); тексты — через внешние источники",
        )

    async def search_statutes(
        self, query: str, jurisdiction: str, limit: int = 8
    ) -> list[LegalSource]:
        hits = await _to_thread(search_documents, query, limit=limit)
        sources = [hit_to_legal_source(hit, seq=i + 1) for i, hit in enumerate(hits)]
        if not self._enrich_text or not sources:
            return sources
        # Добор текста статьи (best effort) — только для запросов вида
        # «статья N …»: берём топ-1 источник, текст не критичен.
        article_num = extract_article_number(query)
        if article_num:
            base_hint = extract_base_law_hint(query)
            try:
                article = await _to_thread(
                    find_consultant_article, article_num, base_hint
                )
                top = sources[0]
                top.excerpt = article.text[:4000]
                top.warning = (
                    "текст статьи получен с КонсультантПлюс (неофициальный источник); "
                    "реквизиты подтверждены pravo.gov.ru — сверьте текст по официальной ссылке"
                )
            except Exception:  # noqa: BLE001 — текст не критичен, есть реквизиты
                pass
        return sources

    async def get_document(self, source_id: str) -> LegalSource | None:
        # source_id у этого провайдера — eoNumber (номер опубликования).
        return None  # полный документ отдельным методом появится в шаге 5

    async def search_case_law(
        self, query: str, jurisdiction: str, limit: int = 8
    ) -> list[LegalSource]:
        return []  # честно: практики на портале публикации нет (шаг 8)


def extract_article_number(query: str) -> str | None:
    """«статья 309 ГК РФ» → «309»; «ст. 6.1.1 КоАП РФ» → «6.1.1»; иначе None."""
    match = _ARTICLE_NUM_RE.search(query)
    return match.group(1) if match else None


def extract_base_law_hint(query: str) -> str:
    """Выделить подсказку базового акта: «статья 309 ГК РФ» → «ГК РФ»."""
    match = re.search(r"(ГК РФ|ГПК РФ|АПК РФ|ТК РФ|КоАП РФ|НК РФ|СК РФ|ЗоЗПП)", query)
    if match:
        return match.group(1)
    # Иначе — весь запрос без статьи (для «О защите прав потребителей»).
    cleaned = _ARTICLE_NUM_RE.sub("", query).strip(" ,.")
    return cleaned or query



async def _to_thread(func, *args, **kwargs):
    """Запустить блокирующую функцию в потоке (для async-контракта провайдера)."""
    import asyncio

    return await asyncio.to_thread(func, *args, **kwargs)

