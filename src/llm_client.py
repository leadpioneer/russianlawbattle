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

#: Задержки между повторными попытками (сек) при транзитных сбоях роутера:
#: пустой стрим без контента или ошибка API до начала генерации (429/5xx/таймаут).
_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0)

#: HTTP-статусы, при которых повтор запроса имеет смысл (транзитные сбои роутера).
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _is_retryable_api_error(exc: APIError) -> bool:
    """Транзитная ли ошибка API: таймаут/обрыв соединения (без статуса) или 429/5xx."""
    status = getattr(exc, "status_code", None)
    return True if status is None else status in _RETRYABLE_STATUS_CODES


def _empty_reason(finish_reason: str | None, reasoning_chars: int, usage: TokenUsage) -> str:
    """Человекочитаемая причина пустого ответа модели (для лога и исключения)."""
    parts: list[str] = []
    if reasoning_chars:
        parts.append(f"{reasoning_chars} симв. пришло в reasoning-полях без текста реплики")
    if finish_reason:
        parts.append(f"finish_reason={finish_reason}")
    if usage.total_tokens:
        parts.append(f"токены {usage.input_tokens}/{usage.output_tokens}")
    return "; ".join(parts) or "стрим закрыт роутером без контента"


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
    :raises RuntimeError: ошибка API или пустой ответ модели
        (после повторных попыток — транзитные сбои ретраятся автоматически).
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
    attempts = len(_RETRY_DELAYS) + 1

    for attempt in range(1, attempts + 1):
        chunks: list[str] = []
        usage = EMPTY_USAGE
        finish_reason: str | None = None
        reasoning_chars = 0
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
                    choice = event.choices[0]
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    # Некоторые роутеры отдают текст/размышления reasoning-моделей
                    # в extra-полях дельты (reasoning_content и т.п.) — учитываем
                    # в диагностике, но в текст реплики не добавляем.
                    for extra_field in ("reasoning_content", "reasoning"):
                        extra_delta = getattr(delta, extra_field, None) or (
                            getattr(delta, "model_extra", None) or {}
                        ).get(extra_field)
                        if extra_delta:
                            reasoning_chars += len(extra_delta)
                    content = getattr(delta, "content", None)
                    if content:
                        chunks.append(content)
                        if on_delta is not None:
                            on_delta(content)
        except APIError as exc:
            retryable = _is_retryable_api_error(exc)
            if not retryable or attempt >= attempts or chunks:
                # chunks непуст → контент уже ушёл в on_delta/UI: повтор дал бы
                # дубликат реплики, поэтому частично полученный стрим не ретраим.
                raise RuntimeError(
                    f"Ошибка LLM API: роль={role} модель={model} "
                    f"роутер={cfg.api_base_url}: {exc}"
                ) from exc
            delay = _RETRY_DELAYS[attempt - 1]
            logger.warning(
                "LLM API (попытка %d/%d, роль=%s модель=%s): транзитная ошибка, "
                "повтор через %s с: %s",
                attempt,
                attempts,
                role,
                model,
                delay,
                exc,
            )
            time.sleep(delay)
            continue

        text = "".join(chunks).strip()
        if text:
            break  # успешная генерация — выходим из цикла попыток

        # Пустой ответ: транзитный сбой роутера — пробуем ещё раз.
        reason = _empty_reason(finish_reason, reasoning_chars, usage)
        if attempt >= attempts:
            elapsed = time.perf_counter() - started
            raise RuntimeError(
                f"Модель {model} (роль {role}) вернула пустой ответ после {attempts} попыток "
                f"({reason}; суммарно {elapsed:.0f}с). Обычно это транзитный сбой роутера — "
                "перезапустите симуляцию; если повторяется, смените модель роли в конфиге."
            )
        delay = _RETRY_DELAYS[attempt - 1]
        logger.warning(
            "LLM-ответ пуст (попытка %d/%d, роль=%s модель=%s): %s — повтор через %s с",
            attempt,
            attempts,
            role,
            model,
            reason,
            delay,
        )
        time.sleep(delay)

    elapsed = time.perf_counter() - started
    logger.info(
        "LLM-ответ: роль=%s модель=%s роутер=%s длина=%d симв. время=%.1fс "
        "попытка=%d/%d токены=%s",
        role,
        model,
        cfg.api_base_url,
        len(text),
        elapsed,
        attempt,
        attempts,
        f"{usage.input_tokens}/{usage.output_tokens}" if usage.total_tokens else "нет данных",
    )
    _usage_log.append((role, model, usage))
    return LlmResult(text, usage)
