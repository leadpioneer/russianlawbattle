"""Тесты шага 8: case_law — coverage, user-акты, провайдер ВС (mock fixtures)."""

from __future__ import annotations

import pytest

from src.legal.case_law import (
    AUTHORITY_LABELS,
    CaseLawCoverage,
    KadArbitrProvider,
    AtomnoCaseLawProvider,
    UnavailableCaseLawProvider,
    classify_user_document,
    extract_user_act_metadata,
    not_configured_health,
    user_act_to_legal_source,
    user_acts_from_materials,
)
from src.legal.models import EvidencePack, LegalSource, now_iso
from src.legal.service import LegalResearchService, ProviderConfig
from src.legal.supreme_court import (
    SupremeCourtOfficialProvider,
    _parse_plenum_page,
    _relevance,
)


# -------------------------------------------------------------------------
# п.1: UnavailableCaseLawProvider — честная заглушка
# -------------------------------------------------------------------------

async def test_unavailable_provider_returns_empty_and_reports():
    provider = UnavailableCaseLawProvider()
    assert await provider.search_case_law("что угодно", "РФ") == []
    assert await provider.search_statutes("что угодно", "РФ") == []
    assert await provider.get_document("X") is None
    health = await provider.healthcheck()
    assert health.status == "unavailable"
    assert "не подключён" in (health.message or "")


# -------------------------------------------------------------------------
# п.7: disabled-расширения — not_configured, без выдуманных ответов
# -------------------------------------------------------------------------

def test_atomno_provider_is_disabled():
    with pytest.raises(NotImplementedError):
        AtomnoCaseLawProvider()


def test_kad_arbitr_provider_is_disabled():
    with pytest.raises(NotImplementedError):
        KadArbitrProvider()


def test_not_configured_health():
    health = not_configured_health("kad_arbitr")
    assert health.status == "not_configured"
    assert health.capabilities == []


# -------------------------------------------------------------------------
# п.2: классификация загруженных пользователем актов
# -------------------------------------------------------------------------

_ACT_TEXT = (
    "ПОСТАНОВЛЕНИЕ Апелляционной коллегии Верховного Суда РФ от 12 марта 2025 г. "
    "№ 5-АПГ25-10. Суд апелляционной инстанции рассмотрел жалобу. Согласно ст. 309 ГК РФ "
    "обязательства должны исполняться надлежащим образом. Решение районного суда оставить "
    "без изменения."
)
_PLAIN_TEXT = "Договор купли-продажи от 01.02.2025 между ООО «А» и ООО «Б»."


def test_classify_case_law():
    assert classify_user_document("case_files/sud_akt.txt", _ACT_TEXT) == "case_law"
    assert classify_user_document("case_files/dogovor.docx", _PLAIN_TEXT) == "user_document"


def test_extract_user_act_metadata():
    meta = extract_user_act_metadata(_ACT_TEXT)
    assert meta.court and "Верховного Суда" in meta.court
    assert meta.decision_date == "2025-03-12"
    assert meta.case_number and "5-АПГ25-10" in meta.case_number
    assert meta.instance and "апелляцион" in meta.instance.lower()
    assert any("309" in norm for norm in meta.cited_norms)
    assert meta.excerpt


def test_user_act_to_source_is_user_level_unverified():
    source = user_act_to_legal_source("case_files/sud_akt.txt", _ACT_TEXT, seq=1)
    assert source.id == "DOC-001"
    assert source.authority_level == "USER"
    assert source.verification_status == "unverified"
    assert source.verified is False
    assert source.source_type == "case_law"
    assert "не сверены" in (source.warning or "")


def test_user_acts_from_materials():
    class Fragment:
        def __init__(self, source, content):
            self.source = source
            self.content = content

    fragments = [
        Fragment("case_files/sud_akt.txt", _ACT_TEXT),
        Fragment("case_files/dogovor.docx", _PLAIN_TEXT),
    ]
    sources = user_acts_from_materials(fragments)
    assert len(sources) == 1
    assert sources[0].source_type == "case_law"
    assert sources[0].authority_level == "USER"


# -------------------------------------------------------------------------
# п.3: SupremeCourtOfficialProvider (mock-фикстуры страницы vsrf.ru)
# -------------------------------------------------------------------------

#: ТЕСТОВАЯ фикстура HTML (структура повторяет vsrf.ru/plenum.php).
PLENUM_HTML = """
<html><body>
<article><div><span class="sr-only">16 июня 2026</span></div>
<h3>Постановление Пленума Верховного Суда РФ № 17 от 16 июня 2026 г.</h3>
<a href="/upload/iblock/aaa/test17.pdf">Скачать</a></article>
<article><div><span class="sr-only">3 июля 2026</span></div>
<h3>Обзор судебной практики Верховного Суда Российской Федерации № 2 (2026)</h3>
<a href="https://www.vsrf.ru/upload/iblock/bbb/review2.pdf">Скачать</a></article>
<article><div><span>5 сентября 2026</span></div>
<h3>Новость без документа</h3></article>
</body></html>
"""


def _fake_fetch():
    return _parse_plenum_page(PLENUM_HTML)


def test_parse_plenum_page_finds_documents():
    docs = _parse_plenum_page(PLENUM_HTML)
    assert len(docs) == 2  # статья без pdf отфильтрована
    by_type = {d["doc_type"] for d in docs}
    assert "plenum_decision" in by_type
    assert "practice_review" in by_type
    plenum = next(d for d in docs if d["doc_type"] == "plenum_decision")
    assert plenum["url"].endswith(".pdf")
    assert plenum["decision_date"] == "2026-06-16"
    assert plenum["plenum_number"] == "17"


def test_relevance_scoring():
    assert _relevance("Постановление Пленума о защите прав потребителей", "защита прав потребителей") > 0
    assert _relevance("Постановление Пленума", "совершенно иные слова") == 0


async def test_supreme_court_provider_search_with_mock():
    provider = SupremeCourtOfficialProvider(fetch=_fake_fetch)
    # Пленум № 17 — о защите прав потребителей (фикстура заголовка):
    sources = await provider.search_case_law(
        "постановление пленума верховного суда", "РФ", limit=5
    )
    assert sources, "релевантные документы должны найтись"
    for source in sources:
        assert source.official_url.startswith("http")
        assert source.authority_level == "A"
        assert source.verification_status == "verified"
        assert source.decision_date
        assert source.source_type in ("supreme_court", "official_explanation")


async def test_supreme_court_provider_no_match_returns_empty():
    provider = SupremeCourtOfficialProvider(fetch=_fake_fetch)
    sources = await provider.search_case_law("абсолютно нерелевантный запрос", "РФ")
    assert sources == []


async def test_supreme_court_provider_healthcheck_unavailable():
    def _boom():
        raise RuntimeError("сайт недоступен")

    provider = SupremeCourtOfficialProvider(fetch=_boom)
    health = await provider.healthcheck()
    assert health.status == "unavailable"


async def test_supreme_court_provider_search_statutes_empty():
    provider = SupremeCourtOfficialProvider(fetch=_fake_fetch)
    assert await provider.search_statutes("309", "РФ") == []


# -------------------------------------------------------------------------
# п.4: CaseLawCoverage в Evidence Pack и сервисе
# -------------------------------------------------------------------------

def test_coverage_message_after_successful_search():
    coverage = CaseLawCoverage(
        searched_sources=["supreme_court_official"],
        not_searched_sources=["kad_arbitr", "sudact"],
        coverage="official_only",
    )
    message = coverage.user_facing_message()
    assert "поиск выполнен" in message.lower()
    assert "массовый поиск" in message.lower()


def test_coverage_message_without_search():
    coverage = CaseLawCoverage(searched_sources=[], not_searched_sources=["kad_arbitr"])
    message = coverage.user_facing_message()
    assert "массовый поиск" in message.lower()
    assert "не выполнялся" in message.lower()


def test_coverage_roundtrip_in_pack():
    coverage = CaseLawCoverage(
        searched_sources=["supreme_court_official"],
        not_searched_sources=["sudact"],
        coverage="official_only",
        warning="тест",
    )
    pack = EvidencePack(
        case_id="c", jurisdiction="РФ", generated_at=now_iso(),
        case_law_coverage=coverage,
    )
    data = pack.to_dict()
    assert data["case_law_coverage"]["coverage"] == "official_only"
    pack2 = EvidencePack.from_dict(data)
    assert pack2.case_law_coverage is not None
    assert pack2.case_law_coverage.searched_sources == ["supreme_court_official"]


async def test_research_includes_coverage_and_searched_sources():
    configs = [ProviderConfig(name="mock", priority=100, timeout_seconds=5)]
    service = LegalResearchService(provider_configs=configs)
    result = await service.research([""], "РФ", case_id="test")
    assert result.case_law_coverage is not None
    assert result.case_law_coverage.searched_sources == ["mock"]
    assert result.pack.case_law_coverage is result.case_law_coverage


async def test_research_coverage_unavailable_without_case_law_providers():
    service = LegalResearchService(provider_configs=[])
    result = await service.research([""], "РФ")
    assert result.case_law_coverage.coverage == "unavailable"
    message = result.case_law_coverage.user_facing_message()
    assert "не выполнялся" in message.lower()


def test_authority_labels_documented():
    for level in ("A", "B", "C", "D", "USER"):
        assert level in AUTHORITY_LABELS

