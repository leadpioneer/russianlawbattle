"""Тестовые фикстуры: фейковая MCP-сессия для diagnostics без сети.

Помечено как ТЕСТОВЫЕ данные: реквизиты фиктивны и не должны попадать в
production-код или отчёты.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeTool:
    """Минимальный аналог mcp.types.Tool."""

    name: str
    inputSchema: dict[str, Any] = field(default_factory=dict)  # noqa: N815 — как в SDK


@dataclass
class FakeTextContent:
    text: str


@dataclass
class FakeToolResult:
    """Минимальный аналог CallToolResult: structuredContent / content."""

    structuredContent: Any = None  # noqa: N815 — как в SDK
    content: list[Any] = field(default_factory=list)


@dataclass
class FakeSession:
    """Фейковая MCP-сессия: списки tools и ответы на call_tool по имени."""

    tools: list[FakeTool] = field(default_factory=list)
    responses: dict[str, Any] = field(default_factory=dict)  # tool -> payload | Exception
    initialize_delay: float = 0.0

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> Any:
        class _Resp:
            pass

        resp = _Resp()
        resp.tools = [FakeTool(name=t.name) for t in self.tools]
        return resp

    async def call_tool(self, name: str, args: dict[str, Any]) -> FakeToolResult:
        if name not in self.responses:
            raise RuntimeError(f"tool {name} не замокан")
        payload = self.responses[name]
        if isinstance(payload, Exception):
            raise payload
        return FakeToolResult(structuredContent={"result": payload})
