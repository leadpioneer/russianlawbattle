"""Тесты стоимости/валюты: currency_for_base_url и build_cost_summary."""

from __future__ import annotations

from src.graph import DebateResult
from src.session_store import build_cost_summary, currency_for_base_url


class _Usage:
    def __init__(self):
        from src.llm_client import TokenUsage

        self._u = TokenUsage(input_tokens=100, output_tokens=50)

    def as_dict(self):
        return self._u.as_dict()

    def __getattr__(self, name):
        return getattr(self._u, name)


def _result():
    from src.config import load_config

    return DebateResult(
        cfg=load_config(),
        materials=object(),
        history=[],
        verdict="v",
        rounds_played=1,
        finished_by_judge=True,
        usage_log=[("claimant", "test-model", _Usage())],
    )


def test_currency_routerai_is_rub():
    assert currency_for_base_url("https://api.routerai.ru/v1") == "RUB"


def test_currency_openai_is_usd():
    assert currency_for_base_url("https://api.openai.com/v1") == "USD"


def test_currency_empty_defaults_usd():
    assert currency_for_base_url("") == "USD"


def test_cost_summary_has_currency_field():
    from src.llm_client import ModelPricing

    pricing = {"test-model": ModelPricing(prompt=1.0, completion=2.0, cache_read=0.0)}
    summary = build_cost_summary(
        _result(), pricing,
        base_url="https://api.routerai.ru/v1",
    )
    assert summary["currency"] == "RUB"
    assert summary["cost_available"] is True
    assert summary["total_cost_usd"] is not None
