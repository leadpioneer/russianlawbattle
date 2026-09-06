"""Тесты шага 3: провайдер pravo_gov + converters (сеть замокана)."""

from __future__ import annotations

import httpx
import pytest

from src.legal import converters
from src.legal.providers import pravo_gov
from src.legal.providers.pravo_gov import (
    PravoGovProvider,
    extract_article_number,
    extract_base_law_hint,
    hit_to_legal_source,
)
from src.legal.providers.base import LegalProvider


# ТЕСТОВЫЕ данные (фикстуры): запись поиска портала.
HIT = {
    "id": "test-eid-1",
    "eoNumber": "0001202607260025",
    "name": "О внесении изменения в статью 6 (фикстура)",
    "complexName": "Федеральный закон от 26.07.2026 № 266-ФЗ (фикстура)",
    "number": "266-ФЗ",
    "documentDate": "2026-07-26T00:00:00",
    "pdfFileLength": 100541,
}


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload=None, error=None, *a, **k):
        self._payload = payload or {"items": [HIT], "itemsTotalCount": 1}
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        if self._error:
            raise self._error
        return _FakeResponse(self._payload)


# -------------------------------------------------------------------------
# helpers: extract_article_number / extract_base_law_hint
# -------------------------------------------------------------------------

def test_extract_article_number():
    assert extract_article_number("статья 309 ГК РФ") == "309"
    assert extract_article_number("ст. 6.1.1 КоАП РФ") == "6.1.1"
    assert extract_article_number("Статья 18 ЗоЗПП") == "18"
    assert extract_article_number("потребитель вправе вернуть") is None


def test_extract_base_law_hint():
    assert extract_base_law_hint("статья 309 ГК РФ") == "ГК РФ"
    assert "КоАП" in extract_base_law_hint("ст. 6.1.1 КоАП РФ")


def test_extract_base_law_hint_fallback():
    hint = extract_base_law_hint("статья 6 о защите прав потребителей")
    assert "защите прав потребителей" in hint


# -------------------------------------------------------------------------
# search_documents (httpx замокан)
# -------------------------------------------------------------------------

def test_search_documents_success(monkeypatch):
    monkeypatch.setattr(pravo_gov.httpx, "Client", _FakeClient)
    hits = pravo_gov.search_documents("статья 6 ЗоЗПП", limit=5)
    assert len(hits) == 1
    assert hits[0].eo_number == "0001202607260025"
    assert hits[0].number == "266-ФЗ"


def test_search_documents_network_error(monkeypatch):
    monkeypatch.setattr(
        pravo_gov.httpx, "Client",
        lambda *a, **k: _FakeClient(error=httpx.ConnectError("сеть недоступна")),
    )
    with pytest.raises(Exception):
        pravo_gov.search_documents("что-то", limit=5)


# -------------------------------------------------------------------------
# hit_to_legal_source: инварианты верификации
# -------------------------------------------------------------------------

def test_hit_to_source_is_never_verified():
    from src.legal.providers.pravo_gov import PravoHit

    hit = PravoHit(
        eid="x", eo_number="0001", name="тест", complex_name="Тестовый акт (фикстура)",
        number="1-ФЗ", document_date="2026-01-01T00:00:00", pdf_file_length=1,
    )
    source = hit_to_legal_source(hit, seq=1)
    assert source.verified is False
    assert source.verification_status == "partially_verified"
    assert source.excerpt == ""  # текст появляется только через converters
    assert source.official_url and "publication.pravo.gov.ru" in source.official_url


# -------------------------------------------------------------------------
# PravoGovProvider: протокол, healthcheck, деградация, обогащение
# -------------------------------------------------------------------------

async def test_provider_satisfies_protocol():
    assert isinstance(PravoGovProvider(), LegalProvider)


async def test_provider_healthcheck_unavailable(monkeypatch):
    def _boom(*a, **k):
        raise httpx.ConnectError("сеть недоступна")

    monkeypatch.setattr(pravo_gov, "search_documents", _boom)
    health = await PravoGovProvider().healthcheck()
    assert health.status == "unavailable"
    assert health.transport == "direct_api"


async def test_provider_search_without_text_enrichment(monkeypatch):
    monkeypatch.setattr(pravo_gov.httpx, "Client", _FakeClient)
    provider = PravoGovProvider(enrich_text=False)
    sources = await provider.search_statutes("266-ФЗ", "РФ", limit=3)
    assert len(sources) == 1
    assert sources[0].excerpt == ""
    assert sources[0].verification_status == "partially_verified"


async def test_provider_case_law_is_honest_empty():
    assert await PravoGovProvider().search_case_law("что угодно", "РФ") == []


async def test_provider_text_enrichment_via_converter(monkeypatch):
    """Текст статьи добирается через converters (замоканы) и попадает в excerpt."""
    monkeypatch.setattr(pravo_gov.httpx, "Client", _FakeClient)

    from src.legal.converters import ArticleText

    def fake_find(article_number, base_hint):
        return ArticleText(
            text="# Тест Статья 6\n\nТочный текст нормы (фикстура).",
            source_url="https://consultant.test/x",
            provider="consultant",
        )

    monkeypatch.setattr(pravo_gov, "find_consultant_article", fake_find)
    monkeypatch.setattr(pravo_gov, "extract_article_number", lambda q: "6")
    monkeypatch.setattr(pravo_gov, "extract_base_law_hint", lambda q: "ЗоЗПП")

    sources = await PravoGovProvider(enrich_text=True).search_statutes("статья 6 ЗоЗПП", "РФ", limit=3)
    assert sources[0].excerpt.startswith("# Тест Статья 6")
    assert "КонсультантПлюс" in sources[0].warning
    # Статус НЕ поднимается до verified — текст неофициальный!
    assert sources[0].verification_status == "partially_verified"


# -------------------------------------------------------------------------
# converters: очистка markdown (чистые функции)
# -------------------------------------------------------------------------

def test_clean_consultant_markdown_cuts_navigation():
    raw = "\n".join([
        "[Вход в систему](x)",
        "* [Главная](/)",
        "# ГК РФ Статья 309. Общие положения",
        "",
        "Обязательства должны исполняться надлежащим образом.",
        "",
        "Контактная информация 117292, Москва",
        "Мы в социальных сетях",
    ])
    cleaned = converters._clean_consultant_markdown(raw)
    assert cleaned.startswith("# ГК РФ Статья 309")
    assert "надлежащим образом" in cleaned
    assert "Контактная информация" not in cleaned
    assert "социальных сетях" not in cleaned


def test_article_num_regex():
    assert converters._ARTICLE_NUM_RE.search("статья 309 ГК РФ").group(1) == "309"
    assert converters._ARTICLE_NUM_RE.search("ст. 6.1.1 КоАП").group(1) == "6.1.1"
    assert converters._ARTICLE_NUM_RE.search("без номера") is None


