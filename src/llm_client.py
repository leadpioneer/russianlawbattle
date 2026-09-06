"""Универсальный LLM-клиент для ролей (OpenAI-совместимый роутер).

Все вызовы идут через один и тот же роутер (``api_base_url`` + ключ из конфига),
но модель выбирается отдельно для каждой роли (юрист заявителя / юрист
ответчика / судья) — см. ``Config.model_for``.

Каждый вызов логирует, какая модель и какой роутер использовались — это нужно
для дебага и сравнения моделей. Генерация всегда потоковая (streaming):
частичные фрагменты отдаются в колбэк ``on_delta`` (для стриминга в консоль),
итоговый текст возвращается целиком.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from openai import APIError, OpenAI

from .config import Config, load_config

logger = logging.getLogger(__name__)

#: Колбэк для потокового вывода: принимает очередной фрагмент текста.
DeltaCallback = Callable[[str], None]

# --- кэш модуля (клиенты и конфиг переиспользуются между вызовами) ----------
_config: Config | None = None
_clients: dict[str, OpenAI] = {}

#: Таймаут HTTP-запроса к роутеру, секунды (LLM-ответы бывают долгими).
REQUEST_TIMEOUT_SECONDS = 180.0


@dataclass(frozen=True)
class TokenUsage:
    """Потребление токенов одним LLM-вызовом (из usage ответа роутера)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_api(cls, usage: Any) -> "TokenUsage":
        """TokenUsage из объекта usage в формате [OI] (может отсутствовать)."""
        if usage is None:
            return cls()

        def get(obj: Any, key: str) -> Any:
            if obj is None:
                return None
            return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

        input_tokens = int(get(usage, "prompt_tokens") or get(usage, "input_tokens") or 0)
        output_tokens = int(get(usage, "completion_tokens") or get(usage, "output_tokens") or 0)
        total = int(get(usage, "total_tokens") or 0) or (input_tokens + output_tokens)
        details_in = get(usage, "prompt_tokens_details") or get(usage, "input_tokens_details") or {}
        details_out = (
            get(usage, "completion_tokens_details") or get(usage, "output_tokens_details") or {}
        )
        cached = int(get(details_in, "cached_tokens") or 0)
        reasoning = int(
            get(details_out, "reasoning_tokens") or get(details_in, "reasoning_tokens") or 0
        )
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            reasoning_tokens=reasoning,
            total_tokens=total,
        )


EMPTY_USAGE = TokenUsage()


class LlmResult(str):
    """Результат LLM-вызова: текст (str-совместимый) + usage токенов.

    Наследует ``str``: весь код, работающий с текстом реплик (``.strip()``,
    парсинг, конкатенация), продолжает работать без правок. dataclass здесь
    неприменим (конфликт с ``str.__new__``), поэтому конструктор явный.
    """

    usage: TokenUsage

    def __new__(cls, text: str, usage: TokenUsage = EMPTY_USAGE) -> "LlmResult":
        obj = super().__new__(cls, text)
        obj.usage = usage
        return obj

    @property
    def text(self) -> str:
        return str(self)


@dataclass(frozen=True)
class ModelPricing:
    """Цены модели за 1 токен, USD (из GET /models роутера)."""

    prompt: float  # input-токен
    completion: float  # output-токен
    cache_read: float  # кэшированный input-токен


#: Лог потребления за текущую симуляцию: (роль, модель, usage).
_usage_log: list[tuple[str, str, TokenUsage]] = []

#: Кэш прайсов роутера: base_url -> {модель: ModelPricing}.
_pricing_cache: dict[str, dict[str, ModelPricing]] = {}
_pricing_fetched_at: dict[str, float] = {}
_PRICING_TTL_S = 3600.0


def get_usage_log() -> list[tuple[str, str, TokenUsage]]:
    """Потребление токенов за текущую симуляцию (с последнего reset_usage_log)."""
    return list(_usage_log)


def reset_usage_log() -> None:
    """Сбросить лог потребления (в начале каждого run_debate)."""
    _usage_log.clear()


def log_external_usage(role: str, model: str, usage: TokenUsage) -> None:
    """Добавить потребление вне ролевых chat-вызовов (например, веб-поиск sonar)."""
    if usage.total_tokens:
        _usage_log.append((role, model, usage))


def fetch_model_pricing(
    base_url: str,
    api_key: str,
    *,
    timeout_s: float = 15.0,
) -> dict[str, ModelPricing]:
    """Прайсы моделей из GET /models роутера; {} при любой ошибке (деньги — опциональны).

    Результат кэшируется на час (цены меняются редко).
    """
    now = time.monotonic()
    cached_at = _pricing_fetched_at.get(base_url)
    if cached_at is not None and now - cached_at < _PRICING_TTL_S and base_url in _pricing_cache:
        return _pricing_cache[base_url]

    url = f"{base_url.rstrip('/')}/models"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 — отсутствие прайсов не ломает симуляцию
        logger.warning("Прайсы из %s не получены: %s", url, exc)
        return {}

    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        logger.warning("Неожиданный формат ответа /models: %s", type(payload).__name__)
        return {}

    pricing: dict[str, ModelPricing] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "")
        raw = item.get("pricing") or {}
        if not model_id or not isinstance(raw, dict):
            continue
        try:
            pricing[model_id] = ModelPricing(
                prompt=float(raw.get("prompt") or 0.0),
                completion=float(raw.get("completion") or 0.0),
                cache_read=float(raw.get("input_cache_read") or 0.0),
            )
        except (TypeError, ValueError):
            continue
    logger.info("Прайсы роутера: получено %d моделей.", len(pricing))
    _pricing_cache[base_url] = pricing
    _pricing_fetched_at[base_url] = now
    return pricing


def estimate_cost(model: str, usage: TokenUsage, pricing: dict[str, ModelPricing]) -> float | None:
    """Стоимость вызова в USD; None, если прайсов для модели нет."""
    prices = pricing.get(model)
    if prices is None:
        return None
    non_cached_input = max(usage.input_tokens - usage.cached_tokens, 0)
    cost = (
        non_cached_input * prices.prompt
        + usage.cached_tokens * prices.cache_read
        + usage.output_tokens * prices.completion
    )
    return cost


def get_config() -> Config:
    """Загрузить конфиг один раз и кэшировать (см. :func:`reset_clients`)."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(cfg: Config) -> None:
    """Подменить кэшированный конфиг (используется оркестратором при переопределениях)."""
    global _config
    _config = cfg


def reset_clients() -> None:
    """Сбросить кэш конфига и клиентов (для тестов и перечитывания конфига)."""
    global _config
    _clients.clear()
    _config = None


def get_client(role: str) -> OpenAI:
    """OpenAI-совместимый клиент для роли; модель роли проверяется по конфигу.

    :param role: ``claimant_lawyer`` / ``defendant_lawyer`` / ``judge``.
    :raises ConfigError: если роль неизвестна или конфиг не загрузился.
    """
    cfg = get_config()
    model = cfg.model_for(role)  # валидация роли до создания клиента
    if role not in _clients:
        _clients[role] = OpenAI(
            base_url=cfg.api_base_url,
            api_key=cfg.api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        logger.info(
            "Создан LLM-клиент: роль=%s модель=%s роутер=%s", role, model, cfg.api_base_url
        )
    return _clients[role]


def chat(
    role: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    on_delta: DeltaCallback | None = None,
    extra_params: dict[str, Any] | None = None,
) -> str:
    """Один запрос к модели роли: системный промпт + история сообщений.

    Генерация потоковая: фрагменты по мере генерации уходят в ``on_delta``
    (для вывода в консоль), полный текст возвращается в конце.

    :param role: роль агента (определяет модель из конфига).
    :param system_prompt: системный промпт агента.
    :param messages: история диалога (сообщения с ролями ``user``/``assistant``).
    :param temperature: температура сэмплирования.
    :param on_delta: колбэк стриминга; ``None`` — стримить только в лог.
    :param extra_params: доп. параметры запроса (max_tokens, reasoning и т.п.),
        передаются роутеру через ``extra_body``; объединяются с ``cfg.llm_params``.
    :returns: полный текст ответа модели.
    :raises ConfigError: неизвестная роль или проблемы конфига.
    :raises RuntimeError: ошибка API или пустой ответ модели.
    """
    cfg = get_config()
    model = cfg.model_for(role)
    client = get_client(role)
    payload = [{"role": "system", "content": system_prompt}, *messages]
    extra_body: dict[str, Any] = {**cfg.llm_params, **(extra_params or {})}

    logger.info(
        "LLM-запрос: роль=%s модель=%s роутер=%s сообщений=%d temperature=%.1f доп.параметры=%s",
        role,
        model,
        cfg.api_base_url,
        len(messages),
        temperature,
        extra_body or "нет",
    )
    started = time.perf_counter()
    chunks: list[str] = []
    usage = EMPTY_USAGE
    try:
        with client.chat.completions.create(
            model=model,
            messages=payload,
            temperature=temperature,
            stream=True,
            extra_body=extra_body or None,
        ) as stream:
            for event in stream:
                # usage приходит в финальных событиях стрима (stream_options/include_usage
                # или последний chunk у роутеров) — забираем каждый раз, последний победит.
                event_usage = getattr(event, "usage", None)
                if event_usage is not None:
                    usage = TokenUsage.from_api(event_usage)
                if not event.choices:
                    continue
                delta = event.choices[0].delta.content
                if delta:
                    chunks.append(delta)
                    if on_delta is not None:
                        on_delta(delta)
    except APIError as exc:
        raise RuntimeError(
            f"Ошибка LLM API: роль={role} модель={model} роутер={cfg.api_base_url}: {exc}"
        ) from exc

    text = "".join(chunks).strip()
    elapsed = time.perf_counter() - started
    logger.info(
        "LLM-ответ: роль=%s модель=%s роутер=%s длина=%d симв. время=%.1fс токены=%s",
        role,
        model,
        cfg.api_base_url,
        len(text),
        elapsed,
        f"{usage.input_tokens}/{usage.output_tokens}" if usage.total_tokens else "нет данных",
    )
    if not text:
        raise RuntimeError(f"Модель {model} (роль {role}) вернула пустой ответ.")
    _usage_log.append((role, model, usage))
    return LlmResult(text, usage)
