"""Тесты human-in-the-loop: суд запрашивает доказательство → пауза → ответ в истории."""

from __future__ import annotations

import pytest

from src.agents.judge import JudgeDecision, parse_judge_decision
from src import graph as graph_mod


class _StubMaterials:
    context = "тестовый контекст (фикстура)"
    fragments = ()
    summarized = False
    estimated_tokens = 10

    def full_context(self):
        return "тестовый контекст (фикстура)"


def _install_mocks(monkeypatch, decisions: list[JudgeDecision]):
    """Замокать всех агентов; судья возвращает decisions по очереди."""
    from src.agents.base import Statement
    from src.llm_client import LlmResult

    monkeypatch.setattr(graph_mod, "_load_case_cached", lambda cfg: _StubMaterials())

    def _fake_run(cfg, materials, *, free_text_query=None, emit=None):
        pack = graph_mod.EvidencePack(
            case_id="s", jurisdiction="РФ", generated_at="2026-01-01"
        )
        return pack, graph_mod.LegalIssues(source="fallback")

    monkeypatch.setattr(graph_mod, "_run_evidence_pack", _fake_run)

    def _fake_statement(speaker, rnd, text):
        return Statement(speaker=speaker, round=rnd, text=LlmResult(text))

    monkeypatch.setattr(
        graph_mod.claimant_lawyer,
        "make_statement",
        lambda cfg, materials, rnd, history, *, norms_block="", on_delta=None: _fake_statement(
            "Юрист заявителя", rnd, "Требование по ст. 999 ГК РФ."
        ),
    )
    monkeypatch.setattr(
        graph_mod.defendant_lawyer,
        "make_statement",
        lambda cfg, materials, rnd, history, *, norms_block="", on_delta=None: _fake_statement(
            "Юрист ответчика", rnd, "Возражение."
        ),
    )
    decisions_iter = iter(decisions)

    def _fake_review(*a, **k):
        return _fake_statement("Судья", 1, "Оценка раунда."), next(decisions_iter)

    monkeypatch.setattr(graph_mod.judge_agent, "review_round", _fake_review)
    monkeypatch.setattr(
        graph_mod.judge_agent,
        "generate_verdict",
        lambda *a, **k: LlmResult("Решение: иск удовлетворить."),
    )
    monkeypatch.setattr(
        graph_mod.advisor,
        "generate_recommendations",
        lambda *a, **k: graph_mod.advisor.Recommendation(
            target_side="claimant",
            prospects="средние",
            text=LlmResult("Рекомендация."),
        ),
    )


# -------------------------------------------------------------------------
# Парсер маркера ТРЕБУЕТСЯ ДОКАЗАТЕЛЬСТВО
# -------------------------------------------------------------------------


def test_parse_evidence_request_marker():
    text = (
        "Исход дела зависит от причины дефекта.\n"
        "=== РЕШЕНИЕ СУДЬИ: ТРЕБУЕТСЯ ДОКАЗАТЕЛЬСТВО: судебная техническая "
        "экспертиза смартфона — причина самопроизвольных перезагрузок ==="
    )
    d = parse_judge_decision(text)
    assert d.request_evidence
    assert "экспертиз" in d.request_evidence
    assert d.continues is True
    assert not d.requalify
    assert "причины дефекта" in d.question


def test_parse_other_markers_unchanged():
    fin = parse_judge_decision("Достаточно.\n=== РЕШЕНИЕ СУДЬИ: ЗАВЕРШИТЬ ===")
    assert not fin.request_evidence and not fin.requalify

    cont = parse_judge_decision(
        "Вопрос.\n=== РЕШЕНИЕ СУДЬИ: ПРОДОЛЖАТЬ (кому: claimant_lawyer) ==="
    )
    assert not cont.request_evidence and cont.addressee == "claimant_lawyer"


def test_max_evidence_requests_constant():
    assert graph_mod.MAX_EVIDENCE_REQUESTS == 2


def test_debate_result_has_evidence_requests_field():
    import inspect

    sig = inspect.signature(graph_mod.DebateResult.__init__)
    assert "evidence_requests" in sig.parameters


# -------------------------------------------------------------------------
# E2E: судья запрашивает доказательство → колбэк → запись в истории
# -------------------------------------------------------------------------


def test_evidence_request_flow_with_answer(monkeypatch):
    """Суд запрашивает экспертизу → колбэк отдаёт текст → приобщено к истории."""
    from src.config import load_config

    _install_mocks(
        monkeypatch,
        [
            JudgeDecision(
                continues=True,
                addressee="both",
                question="Нужна экспертиза.",
                request_evidence="судебная экспертиза причины перезагрузок",
            ),
            JudgeDecision(continues=False, addressee="", question="Материалов достаточно."),
        ],
    )

    def _fake_wait(request: str) -> str | None:
        assert "экспертиз" in request
        return "Экспертиза установила: недостаток производственный, вины потребителя нет."

    events: list = []
    result = graph_mod.run_debate(
        cfg=load_config(),
        max_rounds=2,
        target_side="claimant",
        sink=events.append,
        wait_for_evidence=_fake_wait,
    )

    records = [s for s in result.history if "приобщено" in s.text]
    assert records, "запись о доказательстве не попала в историю"
    assert "производственный" in records[0].text

    assert len(result.evidence_requests) == 1
    request, answer, provided = result.evidence_requests[0]
    assert "экспертиз" in request
    assert "производственный" in answer
    assert provided is True

    types = [e.type for e in events]
    assert graph_mod.EVENT_EVIDENCE_REQUEST in types
    assert graph_mod.EVENT_EVIDENCE_PROVIDED in types
    req_event = next(e for e in events if e.type == graph_mod.EVENT_EVIDENCE_REQUEST)
    assert "экспертиз" in req_event.payload["request"]


def test_evidence_request_declined(monkeypatch):
    """Колбэк вернул None (не представлено) → запись об этом, процесс идёт дальше."""
    from src.config import load_config

    _install_mocks(
        monkeypatch,
        [
            JudgeDecision(
                continues=True,
                addressee="both",
                question="Нужна выписка.",
                request_evidence="банковская выписка о переводе",
            ),
            JudgeDecision(continues=False, addressee="", question="Достаточно."),
        ],
    )
    result = graph_mod.run_debate(
        cfg=load_config(),
        max_rounds=2,
        target_side="claimant",
        wait_for_evidence=lambda request: None,
    )
    request, answer, provided = result.evidence_requests[0]
    assert provided is False
    records = [s for s in result.history if "не представлено" in s.text]
    assert records
    assert result.verdict  # вердикт вынесен несмотря на отказ


def test_evidence_request_limit(monkeypatch):
    """После MAX_EVIDENCE_REQUESTS запросы судьи игнорируются."""
    from src.config import load_config

    _install_mocks(
        monkeypatch,
        [
            JudgeDecision(
                continues=True, addressee="both", question="q",
                request_evidence=f"доказательство #{i}",
            )
            for i in range(graph_mod.MAX_EVIDENCE_REQUESTS + 1)
        ]
        + [JudgeDecision(continues=False, addressee="", question="done")],
    )
    calls = {"n": 0}

    def _wait(request: str) -> str | None:
        calls["n"] += 1
        return "ответ"

    result = graph_mod.run_debate(
        cfg=load_config(),
        max_rounds=10,
        target_side="claimant",
        wait_for_evidence=_wait,
    )
    assert calls["n"] == graph_mod.MAX_EVIDENCE_REQUESTS
    assert len(result.evidence_requests) == graph_mod.MAX_EVIDENCE_REQUESTS
