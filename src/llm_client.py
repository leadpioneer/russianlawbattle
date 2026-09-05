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
from typing import Callable

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


def get_config() -> Config:
    """Загрузить конфиг один раз и кэшировать (см. :func:`reset_clients`)."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


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
) -> str:
    """Один запрос к модели роли: системный промпт + история сообщений.

    Генерация потоковая: фрагменты по мере генерации уходят в ``on_delta``
    (для вывода в консоль), полный текст возвращается в конце.

    :param role: роль агента (определяет модель из конфига).
    :param system_prompt: системный промпт агента.
    :param messages: история диалога (сообщения с ролями ``user``/``assistant``).
    :param temperature: температура сэмплирования.
    :param on_delta: колбэк стриминга; ``None`` — стримить только в лог.
    :returns: полный текст ответа модели.
    :raises ConfigError: неизвестная роль или проблемы конфига.
    :raises RuntimeError: ошибка API или пустой ответ модели.
    """
    cfg = get_config()
    model = cfg.model_for(role)
    client = get_client(role)
    payload = [{"role": "system", "content": system_prompt}, *messages]

    logger.info(
        "LLM-запрос: роль=%s модель=%s роутер=%s сообщений=%d temperature=%.1f",
        role,
        model,
        cfg.api_base_url,
        len(messages),
        temperature,
    )
    started = time.perf_counter()
    chunks: list[str] = []
    try:
        with client.chat.completions.create(
            model=model,
            messages=payload,
            temperature=temperature,
            stream=True,
        ) as stream:
            for event in stream:
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
        "LLM-ответ: роль=%s модель=%s роутер=%s длина=%d симв. время=%.1fс",
        role,
        model,
        cfg.api_base_url,
        len(text),
        elapsed,
    )
    if not text:
        raise RuntimeError(f"Модель {model} (роль {role}) вернула пустой ответ.")
    return text
