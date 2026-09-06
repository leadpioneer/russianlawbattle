"""Тесты провайдера sonar_web_search (веб-поиск Perplexity Sonar на роутере)."""

from __future__ import annotations

import pytest

from src.llm_client import TokenUsage
from src.legal.providers.sonar import (
    DEFAULT_SONAR_MODEL,
    SonarWebSearchProvider,
    parse_sonar_answer,
)
from src.legal.service import LegalResearchService, ProviderConfig


# -------------------------------------------------------------------------
# Парсер ответа sonar
# -------------------------------------------------------------------------

SONAR_ANSWER = """Вот что удалось найти:

1. Постановление Пленума ВС РФ от 28.06.2012 № 17 | https://www.vsrf.ru/acts/docs17.pdf | О рассмотрении судами гражданских дел по спорам о защите прав потребителей.
2. ст. 18 Закона РФ «О защите прав потребителей» | https://publication.pravo.gov.ru/Document/View/0001201206280002 | Права потребителя при обнаружении недостатков товара.
3. Обзор практики ВС РФ | https://blog-justice.example.com/post | Неофициальный источник — должен быть отфильтрован.
"""

FALLBACK_ANSWER = (
    "Полезные материалы: [Обзор судебной практики ВС РФ № 2 (2023)]"
    "(https://www.vsrf.ru/DS/document/CLD/2/practice2023.pdf) и прочее."
)


def test_parse_sonar_answer_strict_format():
    parsed = parse_sonar_answer(SONAR_ANSWER, "защита прав потребителей")
    assert len(parsed) == 2  # блог example.com отфильтрован белым списком
    assert parsed[0]["url"].startswith("https://www.vsrf.ru/")
    assert "Пленума" in parsed[0]["title"]
    assert parsed[1]["excerpt"].startswith("Права потребителя")


def test_parse_sonar_answer_markdown_fallback():
    parsed = parse_sonar_answer(FALLBACK_ANSWER, "обзор практики")
    assert len(parsed) == 1
    assert parsed[0]["url"] == "https://www.vsrf.ru/DS/document/CLD/2/practice2023.pdf"
    assert "Обзор" in parsed[0]["title"]


def test_parse_sonar_answer_nothing_found():
    assert parse_sonar_answer("НИЧЕГО НЕ НАЙДЕНО", "запрос") == []
    assert parse_sonar_answer("", "запрос") == []


# -------------------------------------------------------------------------
# Провайдер: пустой запрос, хук _post, честный verification_status
# -------------------------------------------------------------------------


def _fake_post(answer: str, usage: TokenUsage | None = None):
    usage = usage or TokenUsage(input_tokens=100, output_tokens=50)

    def _post(query: str, kind: str) -> tuple[str, TokenUsage]:
        return answer, usage

    return _post


async def test_provider_empty_query_returns_empty():
    provider = SonarWebSearchProvider(post=_fake_post(SONAR_ANSWER))
    assert await provider.search_statutes("", "РФ") == []
    assert await provider.search_case_law("   ", "РФ") == []


async def test_provider_search_statutes_partial_verification():
    provider = SonarWebSearchProvider(post=_fake_post(SONAR_ANSWER))
    sources = await provider.search_statutes("возврат товара", "РФ")
    assert sources
    for s in sources:
        assert s.verification_status == "partially_verified"
        assert s.verified is False
        assert s.provider == "sonar_web_search"
        assert s.warning  # честное предупреждение о сверке


async def test_provider_search_case_law_ids_and_urls():
    provider = SonarWebSearchProvider(post=_fake_post(SONAR_ANSWER))
    sources = await provider.search_case_law("практика по потребспорам", "РФ")
    assert [s.id for s in sources] == ["WEB-001", "WEB-002"]
    assert all(s.source_type == "case_law" for s in sources)


async def test_provider_error_returns_empty():
    def _boom(query: str, kind: str):
        raise RuntimeError("роутер недоступен")

    provider = SonarWebSearchProvider(post=_boom)
    assert await provider.search_statutes("запрос", "РФ") == []
    assert await provider.search_case_law("запрос", "РФ") == []


async def test_provider_healthcheck_with_post_hook():
    provider = SonarWebSearchProvider(post=_fake_post("ок"))
    health = await provider.healthcheck()
    assert health.status == "healthy"
    assert health.transport == "web_search"


# -------------------------------------------------------------------------
# Интеграция с сервисом
# -------------------------------------------------------------------------


def test_sonar_in_default_provider_configs():
    names = [provider.name for _, provider in LegalResearchService().providers]
    assert "sonar_web_search" in names


def test_unknown_sonar_model_env_override(monkeypatch):
    monkeypatch.setenv("SONAR_MODEL", "perplexity/sonar")
    provider = SonarWebSearchProvider()
    assert provider._model == "perplexity/sonar"  # noqa: SLF001 — тест инкапсуляции
    assert DEFAULT_SONAR_MODEL == "perplexity/sonar-pro-search"
