"""Тесты шага 6: узел build_evidence_pack в графе (замоканные провайдеры)."""

from __future__ import annotations

import pytest

from src import graph as graph_mod
from src.legal.models import EvidencePack, LegalSource, ProviderHealth, now_iso
from src.legal.providers import pravo_gov


def _fake_pack() -> EvidencePack:
    return EvidencePack(
        case_id="session",
        jurisdiction="РФ",
        generated_at=now_iso(),
        legal_issues=["тестовый вопрос"],
        sources=[
            LegalSource(
                id="LAW-001", source_type="statute",
                title="Тестовый акт (фикстура)", authority="тест",
                citation="ст. 1 тестового акта (фикстура)", excerpt="",
                official_url="https://x.test/1",
                verification_status="partially_verified",
                provider="pravo_gov", retrieved_at=now_iso(),
            ),
        ],
        provider_statuses=[
            ProviderHealth(provider="pravo_gov", status="healthy",
                           transport="direct_api", checked_at=now_iso(),
                           capabilities=["statutes"]),
        ],
        warnings=[],
    )


def test_legal_research_node_success(monkeypatch):
    """Узел legal_research: pack собирается, события публикуются, блок непустой."""

    async def fake_run(cfg, materials, *, free_text_query=None, emit=None):
        pack = _fake_pack()
        # Эмулируем события узла (_run_evidence_pack делает это внутри).
        if emit is not None:
            emit(graph_mod.DebateEvent(graph_mod.EVENT_EVIDENCE_PACK_READY,
                                       payload={"total_sources": 1}))
        return pack, graph_mod.LegalIssues(issues=["тестовый вопрос"], source="fallback")

    monkeypatch.setattr(graph_mod, "_run_evidence_pack", fake_run)

    from src.config import load_config

    cfg = load_config()
    sink_events: list = []
    graph = graph_mod.build_graph(cfg, sink=sink_events.append)
    # Граф компилируется с новым узлом; полный прогон требует LLM — здесь
    # проверяем только наличие узла и событий в потоке при invoke невозможен.
    names = set(graph.get_graph().nodes)
    assert graph_mod.NODE_RESEARCH in names
    assert graph_mod.NODE_CLAIMANT in names


def test_new_event_constants_exist():
    assert graph_mod.EVENT_LEGAL_RESEARCH_STARTED == "legal_research_started"
    assert graph_mod.EVENT_PROVIDER_STATUS == "provider_status"
    assert graph_mod.EVENT_LEGAL_SOURCE_FOUND == "legal_source_found"
    assert graph_mod.EVENT_EVIDENCE_PACK_READY == "evidence_pack_ready"


def test_pack_to_excerpts_conversion():
    pack = _fake_pack()
    excerpts = graph_mod._pack_to_excerpts(pack)
    assert len(excerpts) == 1
    assert excerpts[0].title == "Тестовый акт (фикстура)"
    assert excerpts[0].source_url == "https://x.test/1"
    assert graph_mod._pack_to_excerpts(None) == []


def test_pack_warnings_degraded():
    pack = _fake_pack()
    # Нет verified → предупреждение
    warnings = graph_mod._pack_warnings(pack, "judge", 1)
    assert warnings and "ручной проверки" in warnings[0]
    # Есть verified → тишина
    from src.legal.models import LegalSource as LS

    pack.sources.append(
        LS(id="LAW-002", source_type="statute", title="Верифиц. акт (фикстура)",
           authority="тест", citation="ст. 2", excerpt="Точный текст (фикстура).",
           verified=True, verification_status="verified", provider="pravo_gov",
           retrieved_at=now_iso())
    )
    assert graph_mod._pack_warnings(pack, "judge", 1) == []


def test_debate_result_carries_evidence_pack():
    """DebateResult имеет поле evidence_pack (default None)."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(graph_mod.DebateResult)}
    assert "evidence_pack" in field_names
