"""CLI-точка входа диагностики: ``python -m src.legal_diagnostics``.

Проверяет цепочку pravo-mcp: пакет → конфиг → transport → сессия → tools →
тестовый поиск → полнота данных документа. Код возврата 1, если есть fail
(удобно для CI/скриптов). Печатает человекочитаемый рендер; ``--json`` — машиночитаемый.
"""

from __future__ import annotations

import sys

import typer

from .legal.diagnostics import DEFAULT_PROBE_QUERY, diagnose

app = typer.Typer(add_completion=False, help="Диагностика правовых источников (pravo-mcp).")


@app.command()
def mcp(
    config_path: str = typer.Option(
        None, "--config", "-c", help="Путь к config.yaml (по умолчанию — в корне проекта)."
    ),
    query: str = typer.Option(
        DEFAULT_PROBE_QUERY, "--query", "-q", help="Тестовый поисковый запрос."
    ),
    json_output: bool = typer.Option(False, "--json", help="Вывести JSON вместо текста."),
    # Задел на шаг 4 (провайдеры); сейчас фиксируем, что флаг распознан.
    provider: str = typer.Option(
        None, "--provider", "-p", help="Провайдер (пока поддерживается только mcp/pravo-mcp)."
    ),
) -> None:
    """Диагностика MCP-интеграции pravo.gov.ru."""
    if provider and provider not in ("mcp", "pravo-mcp", "pravo_mcp"):
        typer.echo(
            f"Провайдер '{provider}' появится на шаге 4 этапа 3; сейчас доступен только mcp.",
            err=True,
        )
        raise typer.Exit(code=2)

    report = diagnose(config_path, probe_query=query)
    typer.echo(report.to_json() if json_output else report.render())
    raise typer.Exit(code=0 if report.ok else 1)


if __name__ == "__main__":
    app()
