"""Тесты шага 4–5: issue_extractor + LegalResearchService + prompts (без сети)."""

from __future__ import annotations

import pytest

from src.legal.issue_extractor import (
    LegalIssues,
    _extract_json_object,
    _fallback_issues,
    extract_issues,
)
from src.legal.models import EvidencePack, LegalSource, now_iso
from src.legal.prompts import (
    format_evidence_block,
    format_provider_status_block,
)
from src.legal.providers.mock import MockLegalProvider
from src.legal.service import (
    LegalResearchService,
    ProviderConfig,
    ResearchResult,
    build_providers,
)


# -------------------------------------------------------------------------
# issue_extractor: JSON-парсинг и fallback
# -------------------------------------------------------------------------

def test_extract_json_object_plain():
    assert _extract_json_object('{"issues": ["а", "б"]}') == {"issues": ["а", "б"]}


def test_extract_json_object_fenced():
    text = "Вот ответ:\n```json\n{\"issues\": [\"вопрос\"]}\n```\n"
    assert _extract_json_object(text) == {"issues": ["вопрос"]}


def test_extract_json_object_garbage():
    assert _extract_json_object("не json вообще") is None
    assert _extract_json_object('{"a": 1') is None  # обрезанный


def test_fallback_issues_from_text():
    issues = _fallback_issues("Продавец продал бракованный товар. Покупатель требует возврат.", "")
    assert issues.source == "fallback"
    assert issues.issues


def test_fallback_issues_empty():
    assert _fallback_issues("", "").issues == []


def test_extract_issues_use_llm_false_with_free_text():
    issues = extract_issues("", "", "РФ", free_text_query="возврат товара", use_llm=False)
    assert issues.source == "user"
    assert issues.issues == ["возврат товара"]


def test_legal_issues_search_queries():
    issues = LegalIssues(
        case_type="ЗоЗПП", issues=["вопрос 1", "вопрос 2"],
    )
    queries = issues.search_queries()
    assert "вопрос 1" in queries and "вопрос 2" in queries
    assert "ЗоЗПП" in queries[-1]


# -------------------------------------------------------------------------
# service: сборка провайдеров, дедупликация, ранжирование
# -------------------------------------------------------------------------

def _source(**overrides) -> LegalSource:
    base = dict(
        id="LAW-000", source_type="statute", title="Акт (фикстура)",
        authority="тест", citation="ст. 1 (фикстура)", excerpt="",
        official_url="https://x.test/1", verification_status="partially_verified",
        provider="mock", retrieved_at=now_iso(),
    )
    base.update(overrides)
    return LegalSource(**base)


def test_build_providers_skips_unknown_and_disabled():
    configs = [
        ProviderConfig(name="pravo_gov", enabled=True, priority=100),
        ProviderConfig(name="нет такого", enabled=True, priority=50),
        ProviderConfig(name="mock", enabled=False, priority=10),
    ]
    pairs = build_providers(configs)
    names = [p.name for _, p in pairs]
    assert names == ["pravo_gov"]


def test_build_providers_sorted_by_priority():
    configs = [
        ProviderConfig(name="mock", priority=200),
        ProviderConfig(name="pravo_gov", priority=100),
    ]
    pairs = build_providers(configs)
    assert pairs[0][1].name == "pravo_gov"
    assert pairs[1][1].name == "mock"


def test_dedupe_prefers_better_status():
    service = LegalResearchService(provider_configs=[])
    dup1 = _source(provider="mock", verification_status="partially_verified")
    dup2 = _source(provider="pravo_gov", verification_status="partially_verified")
    result = service._dedupe([dup1, dup2])
    assert len(result) == 1
    assert result[0].provider == "pravo_gov"


def test_source_sort_key_verified_first():
    key_verified = LegalResearchService._source_sort_key(
        _source(verification_status="verified", verified=True, excerpt="Текст (фикстура).")
    )
    key_partial = LegalResearchService._source_sort_key(_source())
    assert key_verified < key_partial


def test_renumber():
    service = LegalResearchService(provider_configs=[])
    sources = [_source() for _ in range(3)]
    service._renumber(sources, "LAW")
    assert [s.id for s in sources] == ["LAW-001", "LAW-002", "LAW-003"]


# -------------------------------------------------------------------------
# research: end-to-end на mock-провайдерах (без сети)
# -------------------------------------------------------------------------

async def test_research_with_mock_provider():
    configs = [ProviderConfig(name="mock", priority=100, timeout_seconds=5)]
    service = LegalResearchService(provider_configs=configs)
    # Пустой запрос → mock отдаёт все фикстуры (нормы + практику).
    result = await service.research([""], "РФ", case_id="test")
    assert isinstance(result, ResearchResult)
    assert result.pack.sources
    assert any(s.source_type == "statute" for s in result.pack.sources)
    assert any(s.source_type == "supreme_court" for s in result.pack.sources)
    statute_ids = [s.id for s in result.pack.sources if s.source_type == "statute"]
    assert statute_ids[0] == "LAW-001"


async def test_research_degraded_when_provider_fails():
    configs = [ProviderConfig(name="mock", priority=100, timeout_seconds=5)]
    service = LegalResearchService(provider_configs=configs)
    # Подменяем провайдер на «сломанный» через monkeypatch реестра нельзя —
    # поэтому строим сервис вручную с fail-провайдером.
    service.providers = [(configs[0], MockLegalProvider(fail_search=True))]
    result = await service.research(["запрос"], "РФ")
    assert result.pack.sources == [] or result.pack.warnings
    # Pack всё равно вернулся (деградация ≠ исключение).
    assert isinstance(result.pack, EvidencePack)


async def test_research_provider_error_becomes_warning():
    configs = [ProviderConfig(name="mock", priority=100, timeout_seconds=5)]
    service = LegalResearchService(provider_configs=configs)
    service.providers = [(configs[0], MockLegalProvider(fail_search=True))]
    result = await service.research(["запрос"], "РФ")
    assert any("RuntimeError" in w for w in result.pack.warnings)


async def test_research_unknown_provider_warns(caplog):
    configs = [ProviderConfig(name="нет такого", priority=100)]
    service = LegalResearchService(provider_configs=configs)
    result = await service.research(["запрос"], "РФ")
    assert result.pack.sources == []
    assert result.pack.provider_statuses == []


# -------------------------------------------------------------------------
# prompts: формат Evidence Pack
# -------------------------------------------------------------------------

def _pack_with_sources() -> EvidencePack:
    return EvidencePack(
        case_id="c", jurisdiction="РФ", generated_at=now_iso(),
        legal_issues=["вопрос"],
        sources=[
            _source(id="LAW-001", verification_status="verified", verified=True,
                    excerpt="Точный текст нормы (фикстура)."),
            _source(id="LAW-002"),
            _source(id="LAW-003", verification_status="unverified"),
        ],
    )


def test_format_evidence_block_sections():
    block = format_evidence_block(_pack_with_sources())
    assert "ПРОВЕРЕННЫЕ ИСТОЧНИКИ" in block
    assert "ЧАСТИЧНО ПРОВЕРЕННЫЕ" in block
    assert "НЕПРОВЕРЕННЫЕ ИСТОЧНИКИ" in block
    assert "[LAW-001]" in block and "[LAW-003]" in block
    assert "Точный текст нормы" in block  # excerpt verified-источника
    assert "ПРАВИЛА ЦИТИРОВАНИЯ" in block


def test_format_evidence_block_empty_is_degraded():
    pack = EvidencePack(case_id="c", jurisdiction="РФ", generated_at=now_iso())
    block = format_evidence_block(pack)
    assert "недоступны или ничего не найдено" in block
    assert "требует проверки" in block


def test_provider_status_block():
    from src.legal.models import ProviderHealth

    pack = EvidencePack(case_id="c", jurisdiction="РФ", generated_at=now_iso())
    pack.provider_statuses = [
        ProviderHealth(provider="pravo_gov", status="healthy", transport="direct_api",
                       checked_at=now_iso(), capabilities=["statutes"]),
    ]
    text = format_provider_status_block(pack)
    assert "pravo_gov: healthy" in text
    assert "statutes" in text


# -------------------------------------------------------------------------
# evidence_pack: сборка и persist (LLM выключен, сеть замокана на mock)
# -------------------------------------------------------------------------

async def test_build_evidence_pack_offline(tmp_path, monkeypatch):
    from src.legal.evidence_pack import (
        build_evidence_pack_async,
        load_evidence_pack,
        save_evidence_pack,
    )

    configs = [ProviderConfig(name="mock", priority=100, timeout_seconds=5)]
    service = LegalResearchService(provider_configs=configs)
    pack, issues, result = await build_evidence_pack_async(
        "Продавец продал бракованный товар",
        "Договор купли-продажи, претензия",
        "Российская Федерация",
        case_id="sess-1",
        use_llm=False,
        service=service,
    )
    assert pack.sources, "широкий добор должен собрать фикстуры mock-провайдера"
    assert issues.source == "fallback"
    assert result.pack.case_id == "sess-1"

    path = save_evidence_pack(pack, tmp_path)
    assert path.exists()
    loaded = load_evidence_pack(tmp_path)
    assert loaded is not None
    assert len(loaded.sources) == len(pack.sources)


def test_build_evidence_pack_sync_in_thread(tmp_path):
    """Sync-обёртка работает из потока без event loop (как узел графа)."""
    import threading

    from src.legal.evidence_pack import build_evidence_pack
    from src.legal.service import LegalResearchService, ProviderConfig

    results: dict = {}

    def worker():
        configs = [ProviderConfig(name="mock", priority=100, timeout_seconds=5)]
        service = LegalResearchService(provider_configs=configs)
        results["pack"], _, _ = build_evidence_pack(
            "контекст", "материалы", "РФ", use_llm=False, service=service,
        )

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert "pack" in results and results["pack"].sources


def test_build_evidence_pack_sync_forbids_loop():
    from src.legal.evidence_pack import build_evidence_pack

    async def _inner():
        with pytest.raises(RuntimeError, match="event loop"):
            build_evidence_pack("", "", "РФ", use_llm=False)

    import asyncio

    asyncio.run(_inner())


def test_load_evidence_pack_missing(tmp_path):
    from src.legal.evidence_pack import load_evidence_pack

    assert load_evidence_pack(tmp_path) is None


