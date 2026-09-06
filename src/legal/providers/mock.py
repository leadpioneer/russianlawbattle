"""Mock-провайдер: предсказуемые ТЕСТОВЫЕ данные без сети.

Все реквизиты в этом модуле — фикстуры (помечены в title суффиксом
«(фикстура)»). Запрещено использовать в production-коде; служит для тестов
сервиса, верификатора цитат и offline-прогонов diagnostics.
"""

from __future__ import annotations

from typing import Literal

from ..models import LegalSource, ProviderHealth, now_iso, make_id

MOCK_PROVIDER_NAME = "mock"

MOCK_STATUTE_VERIFIED = {
    "id": make_id("LAW", 1),
    "source_type": "statute",
    "title": "Гражданский кодекс РФ (часть первая) (фикстура)",
    "authority": "ГК РФ",
    "citation": "ст. 309 ГК РФ",
    "excerpt": (
        "Обязательства должны исполняться надлежащим образом в соответствии с "
        "условиями обязательства и требованиями закона (тестовая выдержка)."
    ),
    "official_url": "https://publication.pravo.gov.ru/Document/View/TEST-MOCK-1",
    "effective_date": "1995-01-01",
    "verified": True,
    "verification_status": "verified",
    "provider": MOCK_PROVIDER_NAME,
    "relevance_score": 10.0,
}

MOCK_STATUTE_PARTIAL = {
    "id": make_id("LAW", 2),
    "source_type": "statute",
    "title": "Федеральный закон о защите прав потребителей (фикстура)",
    "authority": "ЗоЗПП",
    "citation": "ст. 18 ЗоЗПП",
    "excerpt": "",  # текст не получен — только реквизиты
    "official_url": "https://publication.pravo.gov.ru/Document/View/TEST-MOCK-2",
    "verified": False,
    "verification_status": "partially_verified",
    "provider": MOCK_PROVIDER_NAME,
    "warning": "текст нормы не получен; подтверждены только реквизиты",
    "relevance_score": 6.0,
}

MOCK_CASE_LAW = {
    "id": make_id("CASE", 1),
    "source_type": "supreme_court",
    "title": "Постановление Пленума Верховного Суда РФ (фикстура)",
    "authority": "Верховный Суд РФ",
    "citation": "п. 28 ПП ВС РФ № 17 (фикстура)",
    "excerpt": "Суд разъяснил, что потребитель вправе потребовать возврат (тест).",
    "official_url": "https://vsrf.test/TEST-MOCK-3",
    "decision_date": "2012-06-28",
    "case_number": "17 (фикстура)",
    "court": "Пленум Верховного Суда РФ",
    "verified": True,
    "verification_status": "verified",
    "provider": MOCK_PROVIDER_NAME,
    "authority_level": "A",
    "relevance_score": 8.0,
}

MOCK_STATUTE_UNVERIFIED = {
    "id": make_id("LAW", 3),
    "source_type": "statute",
    "title": "Источник без подтверждения (фикстура)",
    "authority": "неизвестно",
    "citation": "ст. 999 ГК РФ",
    "excerpt": "",
    "verified": False,
    "verification_status": "unverified",
    "provider": MOCK_PROVIDER_NAME,
    "warning": "источник не подтверждён внешней проверкой",
    "relevance_score": 1.0,
}


class MockLegalProvider:
    """Детерминированный провайдер для тестов и offline-диагностики."""

    name = MOCK_PROVIDER_NAME

    def __init__(
        self,
        *,
        health: Literal["healthy", "degraded", "unavailable", "not_configured"] = "healthy",
        statutes: list[LegalSource] | None = None,
        case_law: list[LegalSource] | None = None,
        fail_search: bool = False,
    ) -> None:
        from dataclasses import fields

        self._health = health
        self._fail_search = fail_search
        self._statutes = statutes if statutes is not None else [
            LegalSource(**MOCK_STATUTE_VERIFIED),
            LegalSource(**MOCK_STATUTE_PARTIAL),
            LegalSource(**MOCK_STATUTE_UNVERIFIED),
        ]
        self._case_law = case_law if case_law is not None else [LegalSource(**MOCK_CASE_LAW)]

    async def healthcheck(self) -> ProviderHealth:
        from ..models import now_iso as _now_iso

        capabilities: list[str] = []
        if self._health in ("healthy", "degraded"):
            capabilities = ["statutes", "case_law", "documents"]
        return ProviderHealth(
            provider=self.name,
            status=self._health,
            transport="cache",
            checked_at=_now_iso(),
            capabilities=capabilities,
            message="ТЕСТОВЫЙ провайдер (фикстуры)" if self._health != "not_configured" else None,
        )

    async def search_statutes(self, query: str, jurisdiction: str, limit: int = 8) -> list[LegalSource]:
        if self._fail_search:
            raise RuntimeError("mock: search_statutes сломан")
        return self._filter(self._statutes, query)[:limit]

    async def get_document(self, source_id: str) -> LegalSource | None:
        for s in (*self._statutes, *self._case_law):
            if s.id == source_id:
                return s
        return None

    async def search_case_law(self, query: str, jurisdiction: str, limit: int = 8) -> list[LegalSource]:
        if self._fail_search:
            raise RuntimeError("mock: search_case_law сломан")
        return self._filter(self._case_law, query)[:limit]

    @staticmethod
    def _filter(sources: list[LegalSource], query: str) -> list[LegalSource]:
        q = (query or "").strip().lower()
        if not q:
            return list(sources)
        return [
            s for s in sources
            if q in s.title.lower() or q in s.citation.lower() or q in s.excerpt.lower()
        ]
