"""Диагностика MCP-интеграции pravo-mcp (шаг 1 этапа 3).

Проверяемая цепочка: пакет → конфигурация → transport → инициализация клиента →
список tools и их сигнатуры → тестовый поиск → полнота ответа для документа.
Каждая проверка возвращает :class:`CheckResult` с машиночитаемым статусом и
предлагаемым действием; итог — :class:`DiagnosticsReport` (JSON + текстовый рендер).

Единственная точка живой сети — ``_run_mcp_session``: тесты подменяют её хуком,
не поднимая подпроцесс и не обращаясь к pravo.gov.ru.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import platform
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from ..config import DEFAULT_CONFIG_PATH

#: Тестовый запрос по умолчанию (требование ТЗ шага 1).
DEFAULT_PROBE_QUERY = "статья 309 ГК РФ"

#: Таймаут одного MCP-вызова при диагностике, сек.
CALL_TIMEOUT_S = 60.0

CheckStatus = Literal["ok", "warn", "fail", "skip"]


@dataclass
class CheckResult:
    """Результат одной проверки диагностики."""

    check: str
    status: CheckStatus
    details: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    error_chain: list[str] = field(default_factory=list)
    suggested_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnosticsReport:
    """Итог диагностики: машиночитаемый JSON + человекочитаемый рендер."""

    target: str
    started_at: str
    duration_s: float
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True, если нет ни одного fail (warn/skip не считаются провалом)."""
        return all(c.status != "fail" for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 2),
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def render(self) -> str:
        """Человекочитаемый текст (без ANSI — для логов и e2e-логов)."""
        icon = {"ok": "[OK]", "warn": "[!!]", "fail": "[XX]", "skip": "[--]"}
        lines = [
            f"Диагностика: {self.target}",
            f"Запуск: {self.started_at}, длительность {self.duration_s:.1f} с, итог: "
            + ("ПРОБЛЕМ НЕ НАЙДЕНО" if self.ok else "ЕСТЬ ПРОБЛЕМЫ (см. fail)"),
            "=" * 72,
        ]
        for c in self.checks:
            lines.append(f"{icon[c.status]} {c.check}: {c.status.upper()}")
            for key, value in c.details.items():
                lines.append(f"      {key}: {value}")
            if c.error_type:
                lines.append(f"      ошибка: {c.error_type}")
            for i, cause in enumerate(c.error_chain, 1):
                lines.append(f"      причина {i}: {cause}")
            if c.suggested_action:
                lines.append(f"      действие: {c.suggested_action}")
        lines.append("=" * 72)
        return "\n".join(lines)


def mask_secret(value: str | None) -> str | None:
    """Маска секрета для безопасного вывода: первые 5 + последние 4 символа."""
    if not value:
        return None
    if len(value) <= 9:
        return "***"
    return f"{value[:5]}…{value[-4:]}"


def read_legal_mcp_section(config_path: str | Path | None = None) -> dict[str, Any]:
    """Секция ``legal_mcp`` из config.yaml; нет файла/секции — дефолты.

    Ключ ``_source`` — откуда прочитано (для diagnostics); сама секция не
    содержит секретов (у pravo-mcp их нет), но на будущее любые значения с
    именем ``*_key``/``api_key`` маскируются.
    """
    import yaml

    path = Path(config_path) if config_path else Path(DEFAULT_CONFIG_PATH)
    if not path.exists():
        return {"_source": f"файл {path} не найден — используются дефолты", "enabled": True}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 — диагностика не должна падать
        return {"_source": f"ошибка чтения {path}: {type(exc).__name__}: {exc}"}
    section = dict(data.get("legal_mcp") or {})
    section.setdefault("enabled", True)
    section["_source"] = str(path)
    for key in list(section):
        if "key" in str(key).lower() and isinstance(section[key], str):
            section[key] = mask_secret(section[key])
    return section


# -------------------------------------------------------------------------
# Живая MCP-сессия (единственная точка сети; тесты подменяют её)
# -------------------------------------------------------------------------

SessionFactory = Callable[..., Any]


def _run_mcp_session(command: tuple[str, ...], actions: Callable[..., Any]) -> Any:
    """Открыть MCP-сессию (stdio) и выполнить ``actions(session, tool_names)``.

    ``actions`` может быть синхронной (вернёт значение как есть) или возвращать
    корутину — тогда она await'ится в цикле сессии (правильный способ делать
    await-вызовы MCP из диагностики). Выделена отдельной функцией-хуком: тесты
    подменяют её на фейковую сессию без сети.
    Бросает исключения mcp SDK / asyncio.TimeoutError — вызывающий классифицирует.
    """
    import asyncio
    import inspect

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _inner() -> Any:
        server_params = StdioServerParameters(command=command[0], args=list(command[1:]))
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), CALL_TIMEOUT_S)
                tools_response = await asyncio.wait_for(session.list_tools(), CALL_TIMEOUT_S)
                names = [t.name for t in getattr(tools_response, "tools", [])]
                outcome = actions(session, names)
                if inspect.iscoroutine(outcome):
                    outcome = await outcome
                return outcome

    return asyncio.run(asyncio.wait_for(_inner(), timeout=CALL_TIMEOUT_S * 3))



def _error_chain(exc: BaseException) -> list[str]:
    """Цепочка причин исключения (raise ... from ...) без секретов."""
    chain: list[str] = []
    seen: set[int] = set()
    cur = exc.__cause__ or exc.__context__
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    return chain


def classify_session_failure(exc: BaseException) -> CheckResult:
    """Классификация падения MCP-сессии по типу ошибки → статус + действие."""
    import asyncio

    name = type(exc).__name__
    if isinstance(exc, asyncio.TimeoutError) or name == "TimeoutError":
        return CheckResult(
            check="mcp_session",
            status="fail",
            error_type="TimeoutError",
            error_chain=_error_chain(exc),
            suggested_action=(
                "Подпроцесс MCP не ответил вовремя: проверьте, что команда запускается "
                "вручную (python -X utf8 -m pravo_mcp.server), и доступность pravo.gov.ru"
            ),
        )
    if name in ("ModuleNotFoundError", "ImportError"):
        return CheckResult(
            check="mcp_session",
            status="fail",
            error_type=name,
            error_chain=_error_chain(exc),
            suggested_action="Модуль MCP-сервера не найден — пакет не установлен или не тот интерпретатор",
        )
    if name in ("FileNotFoundError", "PermissionError", "OSError"):
        return CheckResult(
            check="mcp_session",
            status="fail",
            error_type=name,
            error_chain=_error_chain(exc),
            suggested_action="Не удалось запустить команду MCP-сервера — проверьте legal_mcp.command в config.yaml",
        )
    if name in ("ConnectionError", "HTTPError", "SSLError", "ProtocolError"):
        return CheckResult(
            check="mcp_session",
            status="fail",
            error_type=name,
            error_chain=_error_chain(exc),
            suggested_action="Сетевая проблема до pravo.gov.ru — проверьте доступность портала и прокси",
        )
    return CheckResult(
        check="mcp_session",
        status="fail",
        error_type=name,
        error_chain=_error_chain(exc),
        suggested_action="Смотрите error_chain; запустите команду MCP вручную и сверьте вывод",
    )


# -------------------------------------------------------------------------
# Отдельные проверки
# -------------------------------------------------------------------------

def check_package() -> CheckResult:
    """Проверка 1: установлен ли пакет pravo-mcp и откуда импортируется."""
    try:
        dist_version = importlib.metadata.version("pravo-mcp")
    except importlib.metadata.PackageNotFoundError:
        return CheckResult(
            check="package",
            status="fail",
            details={"package": "pravo-mcp"},
            error_type="PackageNotFoundError",
            suggested_action=(
                "Установите пакет из локального wheel: .venv\\Scripts\\python.exe "
                "-m pip install ./wheels/pravo_mcp-0.1.0-py3-none-any.whl "
                "(или заново запустите install.bat)"
            ),
        )
    try:
        mod = importlib.import_module("pravo_mcp")
        origin = str(getattr(mod, "__file__", "unknown"))
    except Exception as exc:  # noqa: BLE001 — метаданные есть, импорт сломан
        return CheckResult(
            check="package",
            status="fail",
            details={"package": "pravo-mcp", "version": dist_version},
            error_type=type(exc).__name__,
            error_chain=_error_chain(exc),
            suggested_action="Пакет установлен, но не импортируется — проверьте venv и зависимости wheel",
        )
    return CheckResult(
        check="package",
        status="ok",
        details={
            "package": "pravo-mcp",
            "version": dist_version,
            "origin": origin,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        },
    )


def check_config(config_path: str | Path | None = None) -> tuple[CheckResult, dict[str, Any]]:
    """Проверка 2: секция legal_mcp конфига (секреты маскируются)."""
    section = read_legal_mcp_section(config_path)
    enabled = section.get("enabled", True)
    result = CheckResult(
        check="config",
        status="ok" if enabled else "skip",
        details={
            "source": section.get("_source", "?"),
            "enabled": enabled,
            "limit": section.get("limit", 3),
        },
    )
    if not enabled:
        result.details["note"] = "legal_mcp.enabled=false — поиск норм отключён в конфиге"
    return result, section


def check_transport(command: tuple[str, ...]) -> CheckResult:
    """Проверка 3: транспорт подключения (определяется по команде запуска)."""
    first = (command[0] if command else "").lower()
    if "-m" in command or first.endswith((".exe", "python", "python3")):
        transport = "stdio"
    elif first.startswith(("http://", "https://")):
        transport = "http"
    else:
        transport = "unknown"
    return CheckResult(
        check="transport",
        status="ok" if transport in ("stdio", "http") else "warn",
        details={"transport": transport, "command": list(command)},
        suggested_action=(
            None if transport != "unknown"
            else "Неизвестный транспорт — проверьте legal_mcp.command в config.yaml"
        ),
    )


def _unwrap_result(result: Any) -> Any:
    """Достать полезную нагрузку из CallToolResult (mcp SDK / fastmcp)."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    raise RuntimeError("MCP-инструмент вернул пустой ответ")


def _normalize_hits(payload: Any) -> list[dict[str, Any]]:
    """Привести ответ search_npa к списку словарей (аналог legal_tools)."""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("result") or payload.get("documents") or []
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


async def _call_tool(session: Any, tool: str, args: dict[str, Any]) -> Any:
    """await-обёртка session.call_tool с таймаутом."""
    import asyncio

    return await asyncio.wait_for(session.call_tool(tool, args), CALL_TIMEOUT_S)


#: Паттерны «статья N <КОД> РФ» → имя акта для фолбэка поиска (портал ищет
#: подстроку по названию, короткие обозначения кодексов в названиях не встречаются).
_PROBE_FALLBACKS: list[tuple[str, str]] = [
    ("ГК РФ", "Гражданский кодекс"),
    ("ТК РФ", "Трудовой кодекс"),
    ("АПК РФ", "Арбитражный процессуальный кодекс"),
    ("ГПК РФ", "Гражданский процессуальный кодекс"),
    ("КоАП РФ", "об административных правонарушениях"),
    ("ЗоЗПП", "защите прав потребителей"),
]

_ARTICLE_RE = None  # заполняется лениво в _probe_queries


def _probe_queries(query: str) -> list[tuple[str, str]]:
    """Список пробных запросов (строка, пометка фолбэка).

    Первый — запрос как есть; далее (если применимо) — с развёрнутым именем
    акта: «статья 309 ГК РФ» → «статья 309 Гражданский кодекс» → «Гражданский
    кодекс». Портал ищет подстроку по названию акта, поэтому короткие
    обозначения кодексов дают 0 результатов.
    """
    global _ARTICLE_RE
    import re

    if _ARTICLE_RE is None:
        _ARTICLE_RE = re.compile(r"статья\s+\d+", re.IGNORECASE)
    queries = [(query, False)]
    text = query.strip()
    for abbrev, full in _PROBE_FALLBACKS:
        if abbrev.lower() in text.lower():
            expanded = text.replace(abbrev, full).replace(abbrev.lower(), full)
            queries.append((expanded, True))
            queries.append((full, True))
            break
    return queries



def check_mcp_session(
    command: tuple[str, ...], probe_query: str
) -> tuple[CheckResult, CheckResult]:
    """Проверки 4–6: сессия + tools + тестовый search_npa (один прогон сессии)."""
    started = time.monotonic()

    def actions(session: Any, tool_names: list[str]) -> Any:
        async def _inner() -> dict[str, Any]:
            probe: dict[str, Any] = {"tool_names": tool_names}
            if "search_npa" in tool_names:
                probe["search_ok"] = False
                probe["search_error"] = "не выполнялся"
                # Каскад: как есть → с развёрнутым именем акта → только имя акта.
                for q, is_fallback in _probe_queries(probe_query):
                    try:
                        response = await _call_tool(session, "search_npa", {"query": q, "limit": 5})
                        hits = _normalize_hits(_unwrap_result(response))
                    except Exception as exc:  # noqa: BLE001 — фиксируем и продолжаем
                        probe["search_ok"] = False
                        probe["search_error"] = f"{type(exc).__name__}: {exc}"
                        break  # ошибка вызова — каскад бессмыслен, это не «0 хитов»
                    if hits:
                        probe["search_ok"] = True
                        probe["search_response"] = hits
                        probe["search_query_used"] = q
                        probe["search_fallback"] = is_fallback
                        break
                if probe["search_ok"] is False and probe["search_error"] == "не выполнялся":
                    # Все запросы каскада отработали, но 0 хитов везде.
                    probe["search_error"] = ""
            return probe

        return _inner()

    try:
        probe = _run_mcp_session(command, actions)
    except Exception as exc:  # noqa: BLE001 — вся сессия упала
        return classify_session_failure(exc), CheckResult(
            check="search_probe",
            status="skip",
            details={"reason": f"сессия не поднялась: {type(exc).__name__}"},
        )

    duration = round(time.monotonic() - started, 2)
    tool_names: list[str] = probe.get("tool_names", [])
    has_search = "search_npa" in tool_names
    has_get = "get_npa" in tool_names

    session_result = CheckResult(
        check="mcp_session",
        status="ok" if (has_search and has_get) else ("warn" if has_search else "fail"),
        details={
            "tools_found": tool_names,
            "search_npa": has_search,
            "get_npa": has_get,
            "duration_s": duration,
        },
    )
    if not has_search:
        session_result.error_type = "ToolNotFound"
        session_result.suggested_action = (
            "MCP-сервер не содержит search_npa — сверьте версию wheel в wheels/ "
            "и набор методов сервера"
        )
    elif not has_get:
        session_result.details["note"] = (
            "get_npa отсутствует — тексты актов получать неоткуда (только реквизиты)"
        )
    return session_result, _search_probe_result(probe, probe_query, has_search)


def _search_probe_result(
    probe: dict[str, Any], probe_query: str, has_search: bool
) -> CheckResult:
    """Проверка 6 отдельно: тестовый search_npa и полнота полей топ-результата."""
    if not has_search:
        return CheckResult(
            check="search_probe",
            status="skip",
            details={"reason": "search_npa отсутствует в списке tools"},
        )
    if not probe.get("search_ok"):
        err = str(probe.get("search_error", ""))
        if not err:
            # Ошибки не было — все запросы каскада вернули 0 хитов.
            return CheckResult(
                check="search_probe",
                status="fail",
                details={
                    "query": probe_query,
                    "note": "0 результатов по всем вариантам запроса (включая имя акта)",
                },
                suggested_action=(
                    "Портал ищет подстроку по названию акта: переформулируйте запрос "
                    "названием/номером акта (как в legal_tools._prepare_query)"
                ),
            )
        return CheckResult(
            check="search_probe",
            status="fail",
            details={"query": probe_query},
            error_type=err.split(":", 1)[0],
            error_chain=[err],
            suggested_action="Повторите поиск вручную и сверьте ответ сервера (см. error_type)",
        )

    hits = probe.get("search_response") or []
    top = hits[0] if hits else None
    missing: list[str] = []
    if not top:
        missing.append("результатов нет")
    else:
        if not str(top.get("eid") or top.get("id") or ""):
            missing.append("eid/id")
        if not str(top.get("complexName") or top.get("name") or ""):
            missing.append("название")
        if not str(top.get("publicationUrl") or ""):
            missing.append("publicationUrl")
    status: CheckStatus
    if not hits:
        status = "fail"
    elif missing:
        status = "warn"
    else:
        status = "ok"
    result = CheckResult(
        check="search_probe",
        status=status,
        details={
            "query": probe_query,
            "query_used": probe.get("search_query_used"),
            "fallback_used": probe.get("search_fallback", False),
            "hits": len(hits),
            "top_title": (str(top.get("complexName") or top.get("name"))[:120] if top else None),
            "missing_fields": missing or None,
        },
    )
    if not hits:
        result.suggested_action = (
            "Поиск не дал результатов: проверьте формулировку запроса или доступность портала"
        )
    elif missing:
        result.suggested_action = (
            "Ответ сервера неполный — недостающие поля придётся восстанавливать "
            "или помечать источник как partially_verified"
        )
    return result


def check_document_completeness(
    command: tuple[str, ...], probe_query: str
) -> CheckResult:
    """Проверка 7: get_npa для топ-1 — какие поля реально возвращаются."""
    import asyncio

    def actions(session: Any, tool_names: list[str]) -> Any:
        async def _inner() -> dict[str, Any]:
            if "search_npa" not in tool_names:
                return {"reason": "search_npa отсутствует"}
            # Каскад запросов — тот же, что в search_probe.
            hits: list[dict[str, Any]] = []
            for q, _is_fallback in _probe_queries(probe_query):
                response = await _call_tool(session, "search_npa", {"query": q, "limit": 5})
                hits = _normalize_hits(_unwrap_result(response))
                if hits:
                    break
            if not hits:
                return {"reason": "поиск не дал результатов"}
            eid = str(hits[0].get("eid") or hits[0].get("id") or "")
            if not eid:
                return {"reason": "у топ-результата нет eid — get_npa не вызывался"}
            if "get_npa" not in tool_names:
                return {"eid": eid, "reason": "get_npa отсутствует в tools"}
            try:
                detail_response = await _call_tool(session, "get_npa", {"eid": eid})
                return {"eid": eid, "detail": _unwrap_result(detail_response)}
            except Exception as exc:  # noqa: BLE001 — фиксируем тип и текст
                return {"eid": eid, "get_npa_error": f"{type(exc).__name__}: {exc}"}

        return _inner()

    try:
        outcome = _run_mcp_session(command, actions)
    except Exception as exc:  # noqa: BLE001
        return classify_session_failure(exc)

    if outcome.get("reason") and "detail" not in outcome and "get_npa_error" not in outcome:
        return CheckResult(
            check="document_completeness",
            status="skip",
            details={"reason": outcome["reason"]},
        )

    detail = outcome.get("detail")
    get_npa_error = outcome.get("get_npa_error")
    if get_npa_error:
        return CheckResult(
            check="document_completeness",
            status="warn",
            details={"eid": outcome.get("eid"), "get_npa_error": get_npa_error},
            error_type=get_npa_error.split(":", 1)[0],
            suggested_action=(
                "get_npa не удался (известная проблема портала — 404 на тексты). "
                "Источники будут помечаться partially_verified: реквизиты подтверждены, "
                "текст статьи нужно сверять по ссылке"
            ),
        )

    if not isinstance(detail, dict):
        snippet = str(detail)[:200]
        return CheckResult(
            check="document_completeness",
            status="warn",
            details={
                "eid": outcome.get("eid"),
                "detail_type": type(detail).__name__,
                "detail_snippet": snippet,
            },
            suggested_action="Неизвестный формат ответа get_npa — нормализация по фактическим полям",
        )

    # Полнота: название / точная выдержка или текст / ссылка или eid / дата / номер.
    html_content = detail.get("htmlContent") or detail.get("content") or ""
    present = {
        "title": bool(detail.get("complexName") or detail.get("name")),
        "text_or_excerpt": bool(html_content),
        "url_or_eid": bool(detail.get("publicationUrl") or outcome.get("eid")),
        "date": bool(detail.get("documentDate") or detail.get("date")),
        "number": bool(detail.get("number")),
    }
    missing = [k for k, v in present.items() if not v]
    status: CheckStatus = "ok" if not missing else ("warn" if present["url_or_eid"] else "fail")
    return CheckResult(
        check="document_completeness",
        status=status,
        details={
            "eid": outcome.get("eid"),
            "present": present,
            "missing": missing or None,
            "text_length": len(str(html_content)),
        },
        suggested_action=(
            None if not missing
            else "Недостающие поля заполнятся warning'ом; источник помечается partially_verified"
        ),
    )


def diagnose(
    config_path: str | Path | None = None,
    *,
    probe_query: str = DEFAULT_PROBE_QUERY,
) -> DiagnosticsReport:
    """Полный прогон диагностики pravo-mcp. Не бросает исключений."""
    started = time.monotonic()
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    checks: list[CheckResult] = []

    pkg = check_package()
    checks.append(pkg)
    if pkg.status == "fail":
        # Дальше проверять нечего: нет пакета — нет ни конфига, ни транспорта, ни сессии.
        for name in ("config", "transport", "mcp_session", "search_probe", "document_completeness"):
            checks.append(CheckResult(check=name, status="skip", details={"reason": "нет пакета"}))
        return DiagnosticsReport(
            target="pravo-mcp (MCP pravo.gov.ru)",
            started_at=started_at,
            duration_s=time.monotonic() - started,
            checks=checks,
        )

    cfg_result, section = check_config(config_path)
    checks.append(cfg_result)
    raw_command = section.get("command") or [sys.executable, "-X", "utf8", "-m", "pravo_mcp.server"]
    command = tuple(str(part) for part in raw_command)

    checks.append(check_transport(command))

    if cfg_result.status == "skip":  # enabled=false — живые проверки бессмысленны
        for name in ("mcp_session", "search_probe", "document_completeness"):
            checks.append(
                CheckResult(check=name, status="skip", details={"reason": "legal_mcp.enabled=false"})
            )
    else:
        session_result, search_result = check_mcp_session(command, probe_query)
        checks.extend([session_result, search_result])
        checks.append(check_document_completeness(command, probe_query))

    return DiagnosticsReport(
        target="pravo-mcp (MCP pravo.gov.ru)",
        started_at=started_at,
        duration_s=time.monotonic() - started,
        checks=checks,
    )


