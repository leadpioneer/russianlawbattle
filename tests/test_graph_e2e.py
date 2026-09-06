"""E2E полного графа с замоканными агентами — регрессия NameError citation_results.

Пойманный баг: узлы final_verdict/recommendations_node использовали
``citation_results.append(...)`` на несуществующей локальной переменной
вместо возврата значения через reducer (operator.add) — падение
NameError в самом конце прений, после всей генерации.
"""

from __future__ import annotations

import pytest

from src import graph as graph_mod
from src.config import load_config
from src.llm_client import LlmResult


class _StubMaterials:
    context = "тестовый контекст (фикстура)"
    fragments = ()
    summarized = False
    estimated_tokens = 10

    def full_context(self):
        return "тестовый контекст (фикстура)"


@pytest.fixture()
def mock_graph(monkeypatch):
    """Полный граф, где все агенты — замоканные (без LLM и сети)."""
    from src.agents.base import Statement

    monkeypatch.setattr(graph_mod, "_load_case_cached", lambda cfg: _StubMaterials())

    def _fake_run(*a, **k):
        pack = graph_mod.EvidencePack(
            case_id="s", jurisdiction="РФ", generated_at="2026-01-01"
        )
        return pack, graph_mod.LegalIssues(source="fallback")

    monkeypatch.setattr(graph_mod, "_run_evidence_pack", _fake_run)

    def fake_statement(speaker, rnd, text):
        return Statement(speaker=speaker, round=rnd, text=LlmResult(text))

    monkeypatch.setattr(
        graph_mod.claimant_lawyer,
        "make_statement",
        lambda cfg, materials, rnd, history, *, norms_block="", on_delta=None: fake_statement(
            "Юрист заявителя", rnd, "Требование по ст. 999 ГК РФ."
        ),
    )
    monkeypatch.setattr(
        graph_mod.defendant_lawyer,
        "make_statement",
        lambda cfg, materials, rnd, history, *, norms_block="", on_delta=None: fake_statement(
            "Юрист ответчика", rnd, "Возражение по 266-ФЗ."
        ),
    )
    monkeypatch.setattr(
        graph_mod.judge_agent,
        "review_round",
        lambda *a, **k: (
            fake_statement("Судья", 1, "Оценка раунда."),
            graph_mod.judge_agent.JudgeDecision(continues=False, addressee=None, question=""),
        ),
    )
    monkeypatch.setattr(
        graph_mod.judge_agent,
        "generate_verdict",
        lambda *a, **k: LlmResult("Решение: согласно ст. 999 ГК РФ иск удовлетворить."),
    )
    monkeypatch.setattr(
        graph_mod.advisor,
        "generate_recommendations",
        lambda *a, **k: graph_mod.advisor.Recommendation(
            target_side="claimant",
            prospects="средние",
            text=LlmResult("Рекомендация: опирайтесь на подтверждённые источники."),
        ),
    )
    return graph_mod.build_graph(load_config(), sink=None)


def test_full_graph_produces_citation_results(mock_graph):
    """Обе секции linter (verdict + recommendations) доходят до DebateResult."""
    result = graph_mod.run_debate(cfg=load_config(), max_rounds=1, target_side="claimant")
    assert result.citation_results, "citation_results пуст — linter не записался в state"
    sections = {section for section, _ in result.citation_results}
    assert sections == {"verdict", "recommendations"}, f"получено: {sections}"
    assert result.verdict
    assert result.recommendations is not None


def test_no_citation_results_append_in_graph_source():
    """Узлы не должны аппендить в локальную citation_results — только reducer."""
    import inspect

    source = inspect.getsource(graph_mod.build_graph)
    assert "citation_results.append" not in source, (
        "citation_results.append вызывает NameError в рантайме — "
        "используйте возврат значения в state (reducer operator.add)"
    )
