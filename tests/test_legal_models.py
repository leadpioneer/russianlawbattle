"""Тесты шага 2: модели, инварианты верификации, сериализация, MockLegalProvider."""

from __future__ import annotations

import json

import pytest

from src.legal.models import EvidencePack, LegalSource, ProviderHealth, make_id, now_iso
from src.legal.providers.base import LegalProvider
from src.legal.providers.mock import MockLegalProvider


def _source(**overrides) -> LegalSource:
    base = dict(
        id="LAW-001",
        source_type="statute",
        title="ГК РФ часть первая (фикстура)",
        authority="ГК РФ",
        citation="ст. 309 ГК РФ",
        excerpt="Точная выдержка (фикстура).",
        official_url="https://x.test/1",
        verification_status="verified",
        verified=True,
        provider="mock",
        retrieved_at=now_iso(),
    )
    base.update(overrides)
    return LegalSource(**base)


# -------------------------------------------------------------------------
# инварианты верификации — «типы не позволяют выдать непроверенное за проверенное»
# -------------------------------------------------------------------------

def test_verified_requires_status_verified():
    with pytest.raises(ValueError, match="verified"):
        _source(verified=True, verification_status="partially_verified")


def test_verified_requires_excerpt():
    with pytest.raises(ValueError, match="excerpt"):
        _source(verified=True, verification_status="verified", excerpt="")


def test_unverified_allowed_without_excerpt():
    s = _source(verified=False, verification_status="unverified", excerpt="")
    assert s.verification_status == "unverified"
    assert not s.verified


def test_partially_verified_with_requisites_only():
    s = _source(
        verified=False,
        verification_status="partially_verified",
        excerpt="",
        warning="текст не получен",
    )
    assert s.warning


# -------------------------------------------------------------------------
# сериализация
# -------------------------------------------------------------------------

def test_source_roundtrip():
    s = _source(supports_issues=["исполнение обязательств"])
    d = s.to_dict()
    assert json.loads(json.dumps(d, ensure_ascii=False))  # JSON-совместимо
    s2 = LegalSource.from_dict(d)
    assert s2 == s


def test_evidence_pack_roundtrip():
    pack = EvidencePack(
        case_id="case-test",
        jurisdiction="Российская Федерация",
        generated_at=now_iso(),
        legal_issues=["исполнение обязательства"],
        sources=[_source()],
        provider_statuses=[
            ProviderHealth(
                provider="mock", status="healthy", transport="cache",
                checked_at=now_iso(), capabilities=["statutes"],
            )
        ],
        warnings=["тестовое предупреждение"],
    )
    d = pack.to_dict()
    assert json.loads(json.dumps(d, ensure_ascii=False))
    pack2 = EvidencePack.from_dict(d)
    assert pack2.sources[0] == pack.sources[0]
    assert pack2.provider_statuses[0].provider == "mock"


# -------------------------------------------------------------------------
# агрегаты EvidencePack
# -------------------------------------------------------------------------

def test_evidence_pack_aggregates():
    pack = EvidencePack(
        case_id="c",
        jurisdiction="РФ",
        generated_at=now_iso(),
        sources=[
            _source(id="LAW-001", verification_status="verified"),
            _source(id="LAW-002", verified=False, verification_status="partially_verified", excerpt=""),
            _source(id="LAW-003", verified=False, verification_status="unverified", excerpt=""),
        ],
    )
    assert [s.id for s in pack.verified_sources] == ["LAW-001"]
    assert [s.id for s in pack.partially_verified_sources] == ["LAW-002"]
    assert [s.id for s in pack.unverified_sources] == ["LAW-003"]


def test_make_id_format():
    assert make_id("LAW", 1) == "LAW-001"
    assert make_id("CASE", 42) == "CASE-042"


# -------------------------------------------------------------------------
# MockLegalProvider
# -------------------------------------------------------------------------

async def test_mock_provider_satisfies_protocol():
    provider = MockLegalProvider()
    assert isinstance(provider, LegalProvider)  # runtime_checkable


async def test_mock_provider_healthcheck():
    provider = MockLegalProvider()
    health = await provider.healthcheck()
    assert health.status == "healthy"
    assert "statutes" in health.capabilities
    assert "case_law" in health.capabilities


async def test_mock_provider_searches():
    provider = MockLegalProvider()
    statutes = await provider.search_statutes("309", "РФ", limit=5)
    assert any("309" in s.citation for s in statutes)
    case_law = await provider.search_case_law("Пленум", "РФ")
    assert len(case_law) == 1
    assert case_law[0].authority_level == "A"


async def test_mock_provider_verifies_по_статусам():
    provider = MockLegalProvider()
    statutes = await provider.search_statutes("", "РФ")
    statuses = {s.verification_status for s in statutes}
    assert "verified" in statuses
    assert "partially_verified" in statuses
    assert "unverified" in statuses


async def test_mock_provider_get_document():
    provider = MockLegalProvider()
    doc = await provider.get_document("LAW-001")
    assert doc is not None and doc.id == "LAW-001"
    assert await provider.get_document("NOPE") is None


async def test_mock_provider_fail_mode():
    provider = MockLegalProvider(fail_search=True)
    with pytest.raises(RuntimeError):
        await provider.search_statutes("x", "РФ")
