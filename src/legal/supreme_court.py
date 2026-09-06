"""SupremeCourtOfficialProvider — постановления Пленума ВС РФ (шаг 8).

Только официальный домен vsrf.ru. Проверенный живой контракт (06.09.2026):

- ``GET https://www.vsrf.ru/plenum.php`` — страница Пленума; в блоках
  ``<article>`` содержатся заголовки (``<h3>``), даты («16 июня 2026»)
  и ссылки на официальные PDF (``/upload/iblock/…/file.pdf``);
- область MVP: постановления Пленума ВС РФ, обзоры судебной практики ВС РФ
  и опубликованные документы в этих разделах.

Честные ограничения (не заявляем больше, чем есть): полнотекстового поиска
по всей базе судебных актов ВС на сайте нет; провайдер возвращает только
документы раздела Пленума/обзоров, чьи заголовки пересекаются с запросом.
Каждый результат имеет URL, заголовок, дату (если найдена), тип документа
и уровень авторитетности A.
"""

from __future__ import annotations

import re

import httpx

from .models import LegalSource, ProviderHealth, now_iso

VSRF_BASE = "https://www.vsrf.ru"
VSRF_PLENUM_URL = f"{VSRF_BASE}/plenum.php"

_HTTP_TIMEOUT_S = 30.0
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

_MONTHS = {
    "января": "01", "февраля": "02", "марта": "03", "апреля": "04", "мая": "05",
    "июня": "06", "июля": "07", "августа": "08", "сентября": "09", "октября": "10",
    "ноября": "11", "декабря": "12",
}

#: Дата вида «16 июня 2026» в разметке карточки.
_DATE_RE = re.compile(
    r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})",
    re.IGNORECASE,
)

#: Номер постановления Пленума в заголовке: «№ 17».
_PLENUM_NUMBER_RE = re.compile(r"№\s*(\d{1,3})")


def _parse_plenum_page(html: str) -> list[dict]:
    """Разобрать карточки <article> с .pdf; список словарей (для тестов тоже)."""
    documents: list[dict] = []
    seen: set[str] = set()
    for block in re.split(r"<article", html):
        pdf_match = re.search(r'href="([^"]*\.pdf)"', block, re.IGNORECASE)
        if not pdf_match:
            continue
        pdf_href = pdf_match.group(1)
        url = pdf_href if pdf_href.startswith("http") else f"{VSRF_BASE}{pdf_href}"
        if url in seen:
            continue
        seen.add(url)

        title_match = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        raw_title = ""
        if title_match:
            raw_title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
        date_match = _DATE_RE.search(block)
        decision_date = None
        if date_match:
            decision_date = (
                f"{date_match.group(3)}-{_MONTHS[date_match.group(2).lower()]}-{int(date_match.group(1)):02d}"
            )
        number_match = _PLENUM_NUMBER_RE.search(raw_title)

        doc_type = "official_document"
        lowered = raw_title.lower()
        if "постановление пленума" in lowered or "пленум" in lowered:
            doc_type = "plenum_decision"
        if "обзор" in lowered and "практик" in lowered:
            doc_type = "practice_review"

        documents.append(
            {
                "title": raw_title or url.rsplit("/", 1)[-1],
                "url": url,
                "doc_type": doc_type,
                "decision_date": decision_date,
                "plenum_number": number_match.group(1) if number_match else None,
            }
        )
    return documents


def _fetch_plenum_documents() -> list[dict]:
    """Живая загрузка страницы Пленума; RuntimeError при недоступности."""
    response = httpx.get(
        VSRF_PLENUM_URL, timeout=_HTTP_TIMEOUT_S, headers=_UA, follow_redirects=True
    )
    response.raise_for_status()
    return _parse_plenum_page(response.text)


def _relevance(document_title: str, query: str) -> float:
    """Грубое совпадение запроса с заголовком документа."""
    q = query.lower().strip()
    if not q:
        return 1.0
    title = document_title.lower()
    return float(sum(1 for word in re.split(r"\s+", q) if len(word) >= 4 and word in title))


class SupremeCourtOfficialProvider:
    """Официальный провайдер vsrf.ru: Пленум, обзоры, документы (уровень A)."""

    name = "supreme_court_official"

    def __init__(self, *, fetch=None) -> None:
        self._fetch = fetch or _fetch_plenum_documents
        self._last_error: str | None = None

    async def healthcheck(self) -> ProviderHealth:
        import asyncio

        checked_at = now_iso()
        try:
            documents = await asyncio.to_thread(self._fetch)
        except Exception as exc:  # noqa: BLE001 — healthcheck не бросает
            return ProviderHealth(
                provider=self.name,
                status="unavailable",
                transport="direct_api",
                checked_at=checked_at,
                capabilities=[],
                message=f"{type(exc).__name__}: {exc}",
            )
        return ProviderHealth(
            provider=self.name,
            status="healthy" if documents else "degraded",
            transport="direct_api",
            checked_at=checked_at,
            capabilities=["case_law"],
            message=(
                f"официальный сайт ВС РФ доступен; документов раздела: {len(documents)}; "
                "полнотекстовый поиск по всей базе судебных актов ВС не предусмотрен"
            ),
        )

    async def search_statutes(self, query: str, jurisdiction: str, limit: int = 8) -> list[LegalSource]:
        return []  # провайдер практики: нормы ищут другие провайдеры

    async def get_document(self, source_id: str) -> LegalSource | None:
        return None

    async def search_case_law(
        self, query: str, jurisdiction: str, limit: int = 8
    ) -> list[LegalSource]:
        import asyncio

        try:
            documents = await asyncio.to_thread(self._fetch)
        except Exception as exc:  # noqa: BLE001 — ошибка → пусто, причина в health
            self._last_error = f"{type(exc).__name__}: {exc}"
            return []

        self._last_error = None
        scored = sorted(
            ((_relevance(doc["title"], query), doc) for doc in documents),
            key=lambda pair: pair[0],
            reverse=True,
        )
        sources: list[LegalSource] = []
        for score, doc in scored[:limit]:
            if score <= 0 and query.strip():
                continue  # без совпадений — не тащим всё подряд
            doc_type_map = {
                "plenum_decision": "supreme_court",
                "practice_review": "official_explanation",
            }
            sources.append(
                LegalSource(
                    id=f"CASE-{len(sources) + 1:03d}",
                    source_type=doc_type_map.get(doc["doc_type"], "supreme_court"),
                    title=doc["title"],
                    authority="Верховный Суд РФ",
                    citation=doc["title"][:200],
                    excerpt=(
                        f"Официальный документ ВС РФ ({doc['doc_type']}); "
                        "полный текст — в PDF по ссылке на официальном сайте"
                    ),
                    official_url=doc["url"],
                    decision_date=doc["decision_date"],
                    case_number=f"№ {doc['plenum_number']}" if doc["plenum_number"] else None,
                    court="Пленум Верховного Суда РФ",
                    # Публикация на vsrf.ru подтверждает URL/дату/тип документа —
                    # этого достаточно для verified с выдержкой-описанием.
                    verified=True,
                    verification_status="verified",
                    provider="supreme_court_official",
                    retrieved_at=now_iso(),
                    relevance_score=score,
                    supports_issues=[query[:100]] if query else [],
                    authority_level="A",
                )
            )
        return sources

