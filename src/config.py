"""Загрузка и валидация конфигурации «Судебного симулятора».

Источники конфигурации:
  * ``config.yaml`` — роутер (base_url), модели по ролям, юрисдикция, лимиты;
  * ``.env`` — секреты: ключ API (и имя переменной окружения для него).

Ключ API не рекомендуется хранить прямо в ``config.yaml``: файл может попасть
в git. Положите ключ в ``.env`` (см. ``.env.example``), а в ``config.yaml``
укажите имя переменной окружения в ``api_key_env``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

#: Корень проекта (каталог с config.yaml, case_files/, output/).
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

#: Файл с секретами, загружается автоматически, если существует.
ENV_FILE: Path = PROJECT_ROOT / ".env"

#: Путь к конфигу по умолчанию.
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config.yaml"

#: Канонические роли агентов -> имя ключа с моделью в config.yaml.
ROLE_TO_MODEL_KEY: dict[str, str] = {
    "claimant_lawyer": "model_claimant_lawyer",
    "defendant_lawyer": "model_defendant_lawyer",
    "judge": "model_judge",
}

DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_CONTEXT_TOKENS = 12_000
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"

#: Ключи, разрешённые в config.yaml (всё остальное — предупреждение об опечатке).
_ALLOWED_KEYS: set[str] = {
    "api_base_url",
    "api_key",
    "api_key_env",
    *ROLE_TO_MODEL_KEY.values(),
    "jurisdiction",
    "max_rounds",
    "max_context_tokens",
    "llm_params",
    "legal_mcp",
}


class ConfigError(Exception):
    """Некорректная или неполная конфигурация."""


@dataclass(frozen=True)
class Config:
    """Проверенная конфигурация симулятора."""

    api_base_url: str
    api_key: str
    api_key_source: str
    model_claimant_lawyer: str
    model_defendant_lawyer: str
    model_judge: str
    jurisdiction: str
    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    llm_params: dict[str, Any] = field(default_factory=dict)
    legal_mcp: dict[str, Any] = field(default_factory=dict)
    project_root: Path = PROJECT_ROOT

    # --- пути, производные от корня проекта --------------------------------
    @property
    def case_files_dir(self) -> Path:
        """Каталог с документами дела."""
        return self.project_root / "case_files"

    @property
    def case_context_file(self) -> Path:
        """Файл с промпт-контекстом дела (суть спора, позиции сторон)."""
        return self.project_root / "case_context.md"

    @property
    def output_dir(self) -> Path:
        """Каталог для итоговых протоколов/решений."""
        return self.project_root / "output"

    # --- утилиты ------------------------------------------------------------
    def model_for(self, role: str) -> str:
        """Имя модели для роли: ``claimant_lawyer`` / ``defendant_lawyer`` / ``judge``."""
        key = ROLE_TO_MODEL_KEY.get(role.strip().lower())
        if key is None:
            raise ConfigError(
                f"Неизвестная роль: {role!r}. Допустимые роли: {', '.join(ROLE_TO_MODEL_KEY)}."
            )
        return getattr(self, key)

    def masked_key(self) -> str:
        """Ключ API в безопасном для логов виде (первые и последние символы)."""
        if len(self.api_key) <= 8:
            return "***"
        return f"{self.api_key[:5]}…{self.api_key[-4:]}"

    def describe(self) -> str:
        """Человекочитаемое описание конфига без секретов (для CLI и логов)."""
        return "\n".join(
            [
                "Конфигурация судебного симулятора:",
                f"  роутер (base_url)  : {self.api_base_url}",
                f"  ключ API           : {self.masked_key()} (источник: {self.api_key_source})",
                f"  модель заявителя   : {self.model_claimant_lawyer}",
                f"  модель ответчика   : {self.model_defendant_lawyer}",
                f"  модель судьи       : {self.model_judge}",
                f"  юрисдикция         : {self.jurisdiction}",
                f"  max_rounds         : {self.max_rounds}",
                f"  max_context_tokens : {self.max_context_tokens}",
                f"  llm_params         : {self.llm_params if self.llm_params else '(не заданы)'}",
                f"  каталог дела       : {self.case_files_dir}",
                f"  контекст дела      : {self.case_context_file}",
                f"  каталог отчётов    : {self.output_dir}",
            ]
        )


def _require_str(raw: dict[str, Any], key: str) -> str:
    """Обязательный непустой строковый ключ конфига."""
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"В config.yaml отсутствует или пуст обязательный ключ «{key}».")
    return value.strip()


def _as_int(raw: dict[str, Any], key: str, default: int, minimum: int) -> int:
    """Целочисленный ключ конфига со значением по умолчанию и нижней границей."""
    value = raw.get(key, default)
    if isinstance(value, bool):
        raise ConfigError(f"Ключ «{key}» должен быть целым числом, получено: {value!r}.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"Ключ «{key}» должен быть целым числом, получено: {value!r}."
        ) from None
    if number < minimum:
        raise ConfigError(f"Ключ «{key}» должен быть >= {minimum}, получено: {number}.")
    return number


def _resolve_api_key(raw: dict[str, Any]) -> tuple[str, str]:
    """Найти ключ API: сначала в config.yaml (с предупреждением), затем в окружении."""
    inline = raw.get("api_key")
    if isinstance(inline, str) and inline.strip():
        logger.warning(
            "Ключ API указан прямо в config.yaml — это небезопасно (файл может попасть в "
            "git). Рекомендуется перенести его в .env, а в config.yaml оставить api_key_env."
        )
        return inline.strip(), "config.yaml"

    env_name = raw.get("api_key_env")
    if env_name is not None and (not isinstance(env_name, str) or not env_name.strip()):
        raise ConfigError(
            "Ключ «api_key_env» должен быть непустой строкой — именем переменной окружения."
        )
    env_name = (env_name or DEFAULT_API_KEY_ENV).strip()

    key = os.environ.get(env_name)
    if key and key.strip():
        return key.strip(), f"env:{env_name}"

    raise ConfigError(
        "Ключ API не найден.\n"
        "  1) Создайте .env в корне проекта (скопируйте .env.example) и укажите в нём:\n"
        f"         {env_name}=sk-...\n"
        "  2) Либо впишите ключ прямо в config.yaml (api_key: \"...\"), но это небезопасно."
    )


def load_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Загрузить ``.env`` + ``config.yaml``, провалидировать и вернуть :class:`Config`.

    :param config_path: путь к YAML-конфигу (по умолчанию ``config.yaml`` в корне проекта).
    :raises ConfigError: если конфиг отсутствует, не разобран или неполон.
    """
    load_dotenv(ENV_FILE)  # секреты; уже установленные переменные окружения не трогаем

    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(
            f"Конфиг не найден: {path.resolve()}\n"
            "Скопируйте config.yaml.example в config.yaml и заполните его."
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Не удалось разобрать YAML {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(
            f"config.yaml должен содержать словарь «ключ: значение», получено: {type(raw).__name__}."
        )

    unknown = sorted(str(k) for k in set(raw) - _ALLOWED_KEYS)
    if unknown:
        logger.warning(
            "В %s есть неизвестные ключи (возможна опечатка): %s",
            path.name,
            ", ".join(unknown),
        )

    base_url = _require_str(raw, "api_base_url")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"Ключ «api_base_url» должен начинаться с http:// или https://, получено: {base_url!r}."
        )

    api_key, api_key_source = _resolve_api_key(raw)
    models = {role: _require_str(raw, key) for role, key in ROLE_TO_MODEL_KEY.items()}
    jurisdiction = _require_str(raw, "jurisdiction")
    max_rounds = _as_int(raw, "max_rounds", DEFAULT_MAX_ROUNDS, minimum=1)
    max_context_tokens = _as_int(
        raw, "max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS, minimum=1000
    )
    llm_params = raw.get("llm_params", {})
    if not isinstance(llm_params, dict) or not all(isinstance(k, str) for k in llm_params):
        raise ConfigError(
            "Ключ «llm_params» должен быть словарём доп. параметров запроса "
            "(например: max_tokens, reasoning)."
        )
    legal_mcp = raw.get("legal_mcp", {})
    if not isinstance(legal_mcp, dict):
        raise ConfigError(
            "Ключ «legal_mcp» должен быть словарём (enabled, command, limit) — "
            "настройки MCP-сервера норм права."
        )

    logger.info("Конфигурация загружена: %s", path.resolve())
    return Config(
        api_base_url=base_url.rstrip("/"),
        api_key=api_key,
        api_key_source=api_key_source,
        model_claimant_lawyer=models["claimant_lawyer"],
        model_defendant_lawyer=models["defendant_lawyer"],
        model_judge=models["judge"],
        jurisdiction=jurisdiction,
        max_rounds=max_rounds,
        max_context_tokens=max_context_tokens,
        llm_params=dict(llm_params),
        legal_mcp=dict(legal_mcp),
        project_root=PROJECT_ROOT,
    )
