"""CLI-точка входа судебного симулятора: ``python -m src.main run``.

Реплики агентов стримятся в консоль по мере генерации (rich): перед каждым
выступлением печатается заголовок спикера, затем живой текст. Подробные
технические логи (какая модель и роутер обслужили реплику) — по флагу ``-v``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .config import DEFAULT_CONFIG_PATH, load_config
from .graph import run_debate
from .report import save_report

app = typer.Typer(add_completion=False, help="Судебный симулятор: прения трёх LLM-агентов и решение судьи.")
console = Console()


@app.command()
def run(
    max_rounds: Optional[int] = typer.Option(
        None, "--max-rounds", "-r", min=1, help="Переопределить max_rounds из config.yaml (для быстрого прогона)."
    ),
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", exists=True, dir_okay=False, help="Путь к config.yaml (по умолчанию — в корне проекта)."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Не стримить текст реплик в консоль."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Подробные логи (INFO) в stderr."),
) -> None:
    """Запустить симуляцию прений по делу из case_files/ + case_context.md."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    cfg_path = str(config_path) if config_path else None
    console.print(load_config(cfg_path or DEFAULT_CONFIG_PATH).describe(), markup=False, highlight=False)

    def announce(title: str, round_number: int) -> None:
        console.print()
        console.rule(f"[bold cyan]{title}[/bold cyan] · раунд {round_number}")

    def on_delta(chunk: str) -> None:
        # Чанки — сырой текст: пишем напрямую в stdout, без markup-rich.
        sys.stdout.write(chunk)
        sys.stdout.flush()

    result = run_debate(
        max_rounds=max_rounds,
        on_delta=None if quiet else on_delta,
        announce=announce,
        config_path=cfg_path,
    )

    console.print()
    console.rule("[bold yellow]Протокол")
    report_path = save_report(result)
    console.print(
        f"Раундов: [bold]{result.rounds_played}[/bold]; завершено "
        f"{'решением судьи' if result.finished_by_judge else 'лимитом раундов'}."
    )
    console.print(f"Реплик: {len(result.history)}; вердикт: {len(result.verdict)} символов.")
    if result.recommendations is not None:
        console.print(
            f"Рекомендации для стороны [bold]{result.recommendations.side_title}[/bold]: "
            f"перспектива [bold]{result.recommendations.prospects}[/bold] (блок в отчёте)."
        )
    console.print(f"Отчёт: [bold green]{report_path}[/bold green]")


@app.command()
def config(
    config_path: Optional[Path] = typer.Option(
        None, "--config", "-c", exists=True, dir_okay=False, help="Путь к config.yaml."
    ),
) -> None:
    """Показать текущую конфигурацию (без секретов)."""
    console.print(
        load_config(str(config_path) if config_path else DEFAULT_CONFIG_PATH).describe(),
        markup=False,
        highlight=False,
    )


if __name__ == "__main__":
    app()
