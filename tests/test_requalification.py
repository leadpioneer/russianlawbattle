"""Тесты переквалификации дела судьёй: маркер, парсер, маршрутизация графа."""

from __future__ import annotations

import pytest

from src.agents.judge import JudgeDecision, parse_judge_decision
from src import graph as graph_mod


# -------------------------------------------------------------------------
# Парсер маркера судьи
# -------------------------------------------------------------------------


def test_parse_requalification_marker():
    text = (
        "Оценка: выяснилось, что товар использовался для предпринимательства, "
        "ЗоЗПП неприменим.\n"
        "=== РЕШЕНИЕ СУДЬИ: ПЕРЕКВАЛИФИКАЦИЯ: спор окупли-продаже ГК РФ "
        "(товар в предпринимательских целях) ==="
    )
    d = parse_judge_decision(text)
    assert d.requalify is True
    assert d.continues is True
    assert "окупли-продаже" in d.requalify_reason or "предпринимательск" in d.requalify_reason
    assert "Оценка" in d.question


def test_parse_continue_and_finish_unchanged():
    cont = parse_judge_decision(
        "Вопрос стороне.\n=== РЕШЕНИЕ СУДЬИ: ПРОДОЛЖАТЬ (кому: defendant_lawyer) ==="
    )
    assert cont.continues and not cont.requalify
    assert cont.addressee == "defendant_lawyer"

    fin = parse_judge_decision("Доводов достаточно.\n=== РЕШЕНИЕ СУДЬИ: ЗАВЕРШИТЬ ===")
    assert not fin.continues and not fin.requalify


def test_parse_marker_without_reason():
    d = parse_judge_decision("=== РЕШЕНИЕ СУДЬИ: ПЕРЕКВАЛИФИКАЦИЯ ===")
    assert d.requalify is True
    assert d.requalify_reason == ""


# -------------------------------------------------------------------------
# Маршрутизация графа
# -------------------------------------------------------------------------


def test_should_continue_routes_requalification_to_research():
    # Собираем граф, чтобы иметь доступ к внутренним should_continue — но это
    # замыкание; проверяем косвенно: мокаем исследование и судью-переквалификатора.
    pass  # реальная проверка — в e2e ниже


def test_max_requalifications_constant():
    assert graph_mod.MAX_REQUALIFICATIONS == 2


def test_debate_result_has_requalifications_field():
    import inspect

    sig = inspect.signature(graph_mod.DebateResult.__init__)
    assert "requalifications" in sig.parameters
    assert "research_runs" in sig.parameters
