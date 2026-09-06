"""Тесты шага 7: citation verifier (чистые функции, без сети)."""

from __future__ import annotations

from src.legal.citation_verifier import (
    repair_citations,
    verify_citations,
)
from src.legal.models import EvidencePack, LegalSource, now_iso


def _pack() -> EvidencePack:
    return EvidencePack(
        case_id="c", jurisdiction="РФ", generated_at=now_iso(),
        sources=[
            LegalSource(
                id="LAW-001", source_type="statute",
                title="Федеральный закон от 26.07.2026 № 266-ФЗ (фикстура)",
                authority="pravo.gov.ru",
                citation="Федеральный закон от 26.07.2026 № 266-ФЗ",
                excerpt="Точный текст нормы (фикстура).",
                official_url="http://publication.pravo.gov.ru/document/0001202607260025",
                effective_date="2026-07-26",
                verified=True, verification_status="verified",
                provider="pravo_gov", retrieved_at=now_iso(),
            ),
            LegalSource(
                id="LAW-002", source_type="statute",
                title="Частично проверенный акт (фикстура)",
                authority="pravo.gov.ru", citation="ст. 18 ЗоЗПП (фикстура)",
                excerpt="", official_url="http://publication.pravo.gov.ru/document/TEST2",
                verified=False, verification_status="partially_verified",
                provider="pravo_gov", retrieved_at=now_iso(),
                warning="текст не получен",
            ),
            LegalSource(
                id="CASE-001", source_type="supreme_court",
                title="Постановление Пленума ВС (фикстура)", authority="Верховный Суд РФ",
                citation="п. 28 ПП ВС РФ № 17 (фикстура)", excerpt="Текст практики (фикстура).",
                case_number="17 (фикстура)", court="Пленум ВС РФ",
                verified=True, verification_status="verified",
                provider="mock", retrieved_at=now_iso(), authority_level="A",
            ),
        ],
    )


def test_verified_by_evidence_id():
    result = verify_citations("Позиция истца опирается на [LAW-001] и нормы договора.", _pack())
    assert result.overall_status == "passed"
    assert result.verified_count == 1
    assert result.checks[0].evidence_id == "LAW-001"


def test_missing_citation_fails():
    text = "Как установлено ст. 309 ГК РФ, обязательства должны исполняться надлежащим образом."
    result = verify_citations(text, _pack())
    assert result.overall_status == "failed"
    assert result.issue_count == 1
    assert result.checks[0].status == "missing"
    assert "309" in (result.checks[0].citation_text or "")


def test_unverified_source_is_warning():
    text = "Согласно [LAW-002], потребитель вправе требовать возврат."
    result = verify_citations(text, _pack())
    assert result.overall_status == "warning"
    assert result.issue_count == 1
    assert result.checks[0].status == "unverified_source"


def test_act_number_matches_pack():
    text = "Правовое основание — Федеральный закон от 26.07.2026 № 266-ФЗ."
    result = verify_citations(text, _pack())
    assert result.overall_status == "passed"
    assert result.verified_count == 1


def test_missing_act_number():
    text = "Нарушены требования 152-ФЗ о персональных данных."
    result = verify_citations(text, _pack())
    assert result.overall_status == "failed"
    assert result.checks[0].status == "missing"


def test_case_number_missing():
    text = "Аналогичная позиция изложена в деле № А40-12345/2026."
    result = verify_citations(text, _pack())
    assert result.overall_status == "failed"


def test_empty_text_passes():
    result = verify_citations("", _pack())
    assert result.overall_status == "passed"
    assert result.checks == []


def test_no_pack_missing_all():
    text = "Согласно 266-ФЗ и ст. 309 ГК РФ требование обосновано."
    result = verify_citations(text, None)
    assert result.overall_status == "failed"
    assert all(c.status == "missing" for c in result.checks)


def test_to_dict_serializable():
    result = verify_citations("Ссылка на [LAW-001] подтверждена.", _pack())
    data = result.to_dict()
    assert data["overall_status"] == "passed"
    assert data["verified_count"] == 1
    import json

    json.dumps(data, ensure_ascii=False)


def test_repair_pass_no_problems_returns_text():
    result = verify_citations("[LAW-001] подтверждён.", _pack())
    assert repair_citations("текст", result) == "текст"


def test_repair_pass_llm_error_returns_original(monkeypatch):
    text = "Согласно ст. 999 ГК РФ требование обосновано."
    result = verify_citations(text, _pack())
    assert result.overall_status == "failed"

    def _boom(*a, **k):
        raise RuntimeError("LLM недоступна")

    import src.legal.citation_verifier as cv

    def fake_chat(*a, **k):
        raise RuntimeError("LLM недоступна")

    monkeypatch.setattr(
        "src.llm_client.chat", fake_chat
    )
    # repair глотает ошибку LLM и возвращает исходный текст
    assert repair_citations(text, result) == text
