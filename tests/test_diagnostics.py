"""Тесты диагностики pravo-mcp (шаг 1): все сценарии — на замоканной сессии.

Покрывают требования ТЗ: healthy, отсутствует пакет/бинарь, tool не найден,
таймаут, неполный ответ. Фикстуры реквизитов — ТЕСТОВЫЕ данные.
"""

from __future__ import annotations

import asyncio

from src.legal import diagnostics as diag
from tests.fakes import FakeSession, FakeTool, FakeToolResult

#: ТЕСТОВЫЕ данные: фиктивные реквизиты, не для production.
HIT_FULL = {
    "eid": "TEST-EID-1",
    "complexName": "Федеральный закон от 01.01.2026 № 1-ФЗ (тестовая фикстура)",
    "publicationUrl": "https://publication.pravo.gov.ru/Document/View/TEST-EID-1",
    "documentDate": "2026-01-01",
    "number": "1-ФЗ",
}

HEALTHY_TOOLS = [FakeTool(name="search_npa"), FakeTool(name="get_npa")]


def fake_session_run(monkeypatch, session: FakeSession):
    """Подмена _run_mcp_session: выполняет actions на фейковой сессии без сети."""

    def _run(command, actions):
        names = [t.name for t in session.tools]
        return actions(session, names)

    monkeypatch.setattr(diag, "_run_mcp_session", _run)


# -------------------------------------------------------------------------
# package
# -------------------------------------------------------------------------

def test_package_installed_ok(monkeypatch):
    monkeypatch.setattr(diag.importlib.metadata, "version", lambda name: "0.1.0")
    fake_mod = type("M", (), {"__file__": "C:/venv/site-packages/pravo_mcp/__init__.py"})()
    monkeypatch.setattr(diag.importlib, "import_module", lambda name: fake_mod)
    result = diag.check_package()
    assert result.status == "ok"
    assert result.details["version"] == "0.1.0"


def test_package_missing(monkeypatch):
    def _raise(name):
        raise diag.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(diag.importlib.metadata, "version", _raise)
    result = diag.check_package()
    assert result.status == "fail"
    assert result.error_type == "PackageNotFoundError"
    assert "wheel" in (result.suggested_action or "").lower()


def test_package_metadata_ok_but_import_broken(monkeypatch):
    monkeypatch.setattr(diag.importlib.metadata, "version", lambda name: "0.1.0")

    def _raise(name):
        raise ImportError("сломанная зависимость wheel")

    monkeypatch.setattr(diag.importlib, "import_module", _raise)
    result = diag.check_package()
    assert result.status == "fail"
    assert result.error_type == "ImportError"


# -------------------------------------------------------------------------
# config / transport
# -------------------------------------------------------------------------

def test_config_defaults_when_no_file(tmp_path):
    result, section = diag.check_config(tmp_path / "nope.yaml")
    assert result.status == "ok"  # дефолты: включено
    assert section["enabled"] is True


def test_config_disabled(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("legal_mcp:\n  enabled: false\n", encoding="utf-8")
    result, section = diag.check_config(cfg)
    assert result.status == "skip"
    assert section["enabled"] is False


def test_transport_stdio():
    result = diag.check_transport(("python", "-X", "utf8", "-m", "pravo_mcp.server"))
    assert result.status == "ok"
    assert result.details["transport"] == "stdio"


def fake_session_run(monkeypatch, session: FakeSession):
    """Подмена _run_mcp_session: выполняет actions на фейковой сессии без сети."""

    def _run(command, actions):
        import inspect

        names = [t.name for t in session.tools]
        outcome = actions(session, names)
        if inspect.iscoroutine(outcome):
            # asyncio.run нельзя внутри pytest-loop — но тесты синхронные, тут нет loop
            return asyncio.new_event_loop().run_until_complete(outcome)
        return outcome

    monkeypatch.setattr(diag, "_run_mcp_session", _run)


def test_transport_unknown():
    result = diag.check_transport(("weird-binary",))
    assert result.status == "warn"
    assert result.details["transport"] == "unknown"


# -------------------------------------------------------------------------
# mcp_session / search_probe (через замоканную сессию)
# -------------------------------------------------------------------------

def test_session_healthy(monkeypatch):
    session = FakeSession(tools=HEALTHY_TOOLS, responses={"search_npa": [HIT_FULL]})
    fake_session_run(monkeypatch, session)

    session_result, search_result = diag.check_mcp_session(("python",), "тест")
    assert session_result.status == "ok"
    assert session_result.details["search_npa"] is True
    assert session_result.details["get_npa"] is True
    assert search_result.status == "ok"
    assert search_result.details["hits"] == 1


def test_session_tool_not_found(monkeypatch):
    session = FakeSession(tools=[FakeTool(name="другой_tool")], responses={})
    fake_session_run(monkeypatch, session)

    session_result, search_result = diag.check_mcp_session(("python",), "тест")
    assert session_result.status == "fail"
    assert session_result.error_type == "ToolNotFound"
    assert search_result.status == "skip"


def test_session_timeout(monkeypatch):
    def _run(command, actions):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(diag, "_run_mcp_session", _run)
    session_result, search_result = diag.check_mcp_session(("python",), "тест")
    assert session_result.status == "fail"
    assert session_result.error_type == "TimeoutError"
    assert search_result.status == "skip"


def test_session_binary_missing(monkeypatch):
    def _run(command, actions):
        raise FileNotFoundError("python не найден")

    monkeypatch.setattr(diag, "_run_mcp_session", _run)
    session_result, _ = diag.check_mcp_session(("python",), "тест")
    assert session_result.status == "fail"
    assert session_result.error_type == "FileNotFoundError"
    assert "command" in (session_result.suggested_action or "").lower()


def test_search_incomplete_response(monkeypatch):
    hit = {"name": "Без eid и ссылки"}  # нет eid/id, publicationUrl
    session = FakeSession(tools=[FakeTool(name="search_npa")], responses={"search_npa": [hit]})
    fake_session_run(monkeypatch, session)

    _, search_result = diag.check_mcp_session(("python",), "тест")
    assert search_result.status == "warn"
    assert "eid/id" in search_result.details["missing_fields"]
    assert "publicationUrl" in search_result.details["missing_fields"]


def test_search_cascade_fallback(monkeypatch):
    """«статья 309 ГК РФ» даёт 0 хитов, фолбэк по имени акта — находит (как на живом портале)."""

    def fake_call_tool(session, name, args):
        async def _inner():
            if "гк рф" in str(args.get("query", "")).lower():
                return FakeToolResult(structuredContent={"result": []})  # точный запрос — пусто
            return FakeToolResult(structuredContent={"result": [dict(HIT_FULL)]})

        return _inner()

    monkeypatch.setattr(diag, "_call_tool", fake_call_tool)
    session = FakeSession(tools=[FakeTool(name="search_npa")], responses={})
    fake_session_run(monkeypatch, session)

    _, search_result = diag.check_mcp_session(
        ("python",), "статья 309 ГК РФ"
    )
    assert search_result.status == "ok"
    assert search_result.details["fallback_used"] is True
    assert "Гражданский кодекс" in (search_result.details["query_used"] or "")
    assert search_result.details["hits"] == 1


def test_search_tool_raises(monkeypatch):
    session = FakeSession(
        tools=[FakeTool(name="search_npa")],
        responses={"search_npa": RuntimeError("сервер упал")},
    )
    fake_session_run(monkeypatch, session)

    session_result, search_result = diag.check_mcp_session(("python",), "тест")
    assert session_result.status == "warn"  # search есть, get_npa нет
    assert search_result.status == "fail"
    assert search_result.error_type == "RuntimeError"


# -------------------------------------------------------------------------
# document_completeness
# -------------------------------------------------------------------------

def test_document_full(monkeypatch):
    session = FakeSession(
        tools=HEALTHY_TOOLS,
        responses={
            "search_npa": [HIT_FULL],
            "get_npa": {
                "eid": "TEST-EID-1",
                "complexName": HIT_FULL["complexName"],
                "htmlContent": "<p>Статья 1. Тестовая норма (фикстура).</p>",
                "documentDate": "2026-01-01",
                "number": "1-ФЗ",
            },
        },
    )
    fake_session_run(monkeypatch, session)

    result = diag.check_document_completeness(("python",), "тест")
    assert result.status == "ok"
    assert result.details["text_length"] > 0
    assert result.details["missing"] is None


def test_document_get_npa_404(monkeypatch):
    # Известная реальная проблема: search работает, get_npa отдаёт 404.
    session = FakeSession(
        tools=HEALTHY_TOOLS,
        responses={"search_npa": [HIT_FULL], "get_npa": RuntimeError("HTTP 404")},
    )
    fake_session_run(monkeypatch, session)

    result = diag.check_document_completeness(("python",), "тест")
    assert result.status == "warn"
    assert "404" in result.details["get_npa_error"]
    assert "partially_verified" in (result.suggested_action or "")


# -------------------------------------------------------------------------
# отчёт целиком + render + маска секретов
# -------------------------------------------------------------------------

def test_diagnose_report_structure(monkeypatch, tmp_path):
    session = FakeSession(tools=HEALTHY_TOOLS, responses={"search_npa": [HIT_FULL]})
    fake_session_run(monkeypatch, session)

    report = diag.diagnose(tmp_path / "nope.yaml", probe_query="тест")
    names = [c.check for c in report.checks]
    assert names == [
        "package", "config", "transport", "mcp_session", "search_probe", "document_completeness",
    ]
    assert report.ok is True
    assert '"ok": true' in report.to_json()


def test_report_render_contains_all_checks(monkeypatch, tmp_path):
    session = FakeSession(tools=HEALTHY_TOOLS, responses={"search_npa": [HIT_FULL]})
    fake_session_run(monkeypatch, session)

    report = diag.diagnose(tmp_path / "nope.yaml", probe_query="тест")
    text = report.render()
    assert "[OK] package" in text
    assert "[OK] mcp_session" in text


def test_mask_secret():
    assert diag.mask_secret(None) is None
    assert diag.mask_secret("") is None
    assert diag.mask_secret("short") == "***"
    masked = diag.mask_secret("sk-1234567890abcdefghij")
    assert masked.startswith("sk-12")
    assert masked.endswith("ghij")
    assert "67890" not in masked


