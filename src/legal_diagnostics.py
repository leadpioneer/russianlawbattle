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


@app.command()
def mock(
    query: str = typer.Option(
        DEFAULT_PROBE_QUERY, "--query", "-q", help="Тестовый поисковый запрос."
    ),
    json_output: bool = typer.Option(False, "--json", help="Вывести JSON вместо текста."),
) -> None:
    """Smoke-тест mock-провайдера БЕЗ сети (фикстуры, помечены как тестовые)."""
    import asyncio

    from .legal.models import EvidencePack, now_iso
    from .legal.providers.mock import MockLegalProvider

    provider = MockLegalProvider()

    async def _run() -> EvidencePack:
        health = await provider.healthcheck()
        statutes = await provider.search_statutes(query, "Российская Федерация")
        case_law = await provider.search_case_law(query, "Российская Федерация")
        return EvidencePack(
            case_id="mock-smoke",
            jurisdiction="Российская Федерация",
            generated_at=now_iso(),
            legal_issues=[query],
            sources=[*statutes, *case_law],
            provider_statuses=[health],
            warnings=[],
        )

    pack = asyncio.run(_run())
    if json_output:
        typer.echo(pack.to_json())
    else:
        statuses = {s.id: s.verification_status for s in pack.sources}
        typer.echo(f"Провайдер: {provider.name} — {pack.provider_statuses[0].status}")
        for s in pack.sources:
            typer.echo(f"  [{s.id}] {s.verification_status:20s} {s.citation}")
        typer.echo(f"Статусы: {statuses}")
    raise typer.Exit(code=0 if pack.verified_sources else 1)



if __name__ == "__main__":
    app()
