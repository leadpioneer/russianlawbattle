"""Тесты llm_client: ретраи транзитных сбоев (пустой ответ, 429/5xx) без сети."""

from __future__ import annotations

import httpx
import pytest
from openai import APIError, APIStatusError

from src import llm_client
from src.config import Config


# --- фейки [OI]-совместимого стрим-клиента --------------------------------


class _Delta:
    """Минимальный аналог ChoiceDelta (content + extra-поля роутера)."""

    def __init__(self, content: str | None = None, **extra: str | None) -> None:
        self.content = content
        self.model_extra = dict(extra)


class _Choice:
    def __init__(self, delta: _Delta, finish_reason: str | None = None) -> None:
        self.delta = delta
        self.finish_reason = finish_reason


class _Event:
    def __init__(self, choices: list[_Choice], usage: dict | None = None) -> None:
        self.choices = choices
        self.usage = usage


class _Stream:
    """Context manager, возвращающий итератор событий (может кидать исключение)."""

    def __init__(self, events) -> None:
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, *args) -> bool:
        return False


class _Completions:
    """create() отдаёт сценарий: список исходов по вызовам (события | Exception)."""

    def __init__(self, script: list) -> None:
        self.script = script
        self.calls = 0

    def create(self, **kwargs):
        outcome = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return _Stream(outcome)


class _FakeClient:
    """Совместим с обращением client.chat.completions.create в chat()."""

    def __init__(self, script: list) -> None:
        self.completions = _Completions(script)

    @property
    def chat(self):
        return self


# --- фикстуры и хелперы ------------------------------------------------------


def _cfg() -> Config:
    return Config(
        api_base_url="http://routerai.test/api/v1",
        api_key="test-key",
        api_key_source="test",
        model_claimant_lawyer="test/claimant",
        model_defendant_lawyer="test/defendant",
        model_judge="test/judge",
        jurisdiction="Российская Федерация",
    )


def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    """Подменить конфиг и клиент; задержки ретраев — нулевые."""
    monkeypatch.setattr(llm_client, "_RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(llm_client, "get_config", lambda: _cfg())
    monkeypatch.setattr(llm_client, "get_client", lambda role: client)


def _ok_events(text: str = "Позиция ответчика.", usage: dict | None = None) -> list[_Event]:
    return [
        _Event([_Choice(_Delta(content=text))]),
        _Event([_Choice(_Delta(), finish_reason="stop")], usage=usage),
    ]


def _empty_events(usage: dict | None = None) -> list[_Event]:
    return [_Event([_Choice(_Delta(), finish_reason="stop")], usage=usage)]


def _api_error(status: int) -> APIError:
    request = httpx.Request("POST", "http://routerai.test/api/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return APIStatusError(f"HTTP {status}", response=response, body=None)


@pytest.fixture(autouse=True)
def _clean_llm_state():
    llm_client.reset_usage_log()
    yield
    llm_client.reset_clients()
    llm_client.reset_usage_log()


# --- тесты -------------------------------------------------------------------


def test_chat_success_single_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([_ok_events(usage={"prompt_tokens": 10, "completion_tokens": 5})])
    _install(monkeypatch, client)
    deltas: list[str] = []

    result = llm_client.chat(
        "judge", "sys", [{"role": "user", "content": "hi"}], on_delta=deltas.append
    )

    assert result == "Позиция ответчика."
    assert deltas == ["Позиция ответчика."]
    assert client.completions.calls == 1
    assert result.usage.output_tokens == 5
    assert llm_client.get_usage_log() == [("judge", "test/judge", result.usage)]


def test_empty_answer_retried_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([_empty_events(), _ok_events()])
    _install(monkeypatch, client)

    result = llm_client.chat("judge", "sys", [{"role": "user", "content": "hi"}])

    assert result == "Позиция ответчика."
    assert client.completions.calls == 2  # первая попытка пустая, вторая успешная


def test_always_empty_raises_with_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    usage = {"prompt_tokens": 100, "completion_tokens": 0}
    client = _FakeClient([_empty_events(usage=usage)])
    _install(monkeypatch, client)

    with pytest.raises(RuntimeError, match="пустой ответ") as excinfo:
        llm_client.chat("judge", "sys", [{"role": "user", "content": "hi"}])

    message = str(excinfo.value)
    assert "3 попыт" in message  # после всех попыток, не с первой
    assert "finish_reason=stop" in message
    assert client.completions.calls == 3


def test_reasoning_only_answer_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # Роутер отдал только reasoning-поле без текста — ретрай; reasoning не текст реплики.
    reasoning_events = [
        _Event([_Choice(_Delta(reasoning_content="думаю о деле"), finish_reason="stop")])
    ]
    client = _FakeClient([reasoning_events, _ok_events()])
    _install(monkeypatch, client)

    result = llm_client.chat("judge", "sys", [{"role": "user", "content": "hi"}])

    assert result == "Позиция ответчика."
    assert client.completions.calls == 2


def test_transient_api_error_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([_api_error(503), _ok_events()])
    _install(monkeypatch, client)

    result = llm_client.chat("judge", "sys", [{"role": "user", "content": "hi"}])

    assert result == "Позиция ответчика."
    assert client.completions.calls == 2


def test_non_retryable_api_error_raises_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient([_api_error(401), _ok_events()])
    _install(monkeypatch, client)

    with pytest.raises(RuntimeError, match="Ошибка LLM API"):
        llm_client.chat("judge", "sys", [{"role": "user", "content": "hi"}])

    assert client.completions.calls == 1  # 401 не ретраится


def test_partial_stream_error_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # Часть контента уже ушла в on_delta — повтор дал бы дубликат в UI, не ретраим.

    def _partial_then_error():
        yield _Event([_Choice(_Delta(content="начало "))])
        raise _api_error(503)

    client = _FakeClient([_partial_then_error()])  # генератор — исключение при обходе
    _install(monkeypatch, client)
    deltas: list[str] = []

    with pytest.raises(RuntimeError, match="Ошибка LLM API"):
        llm_client.chat(
            "judge", "sys", [{"role": "user", "content": "hi"}], on_delta=deltas.append
        )

    assert client.completions.calls == 1
    assert deltas == ["начало "]

