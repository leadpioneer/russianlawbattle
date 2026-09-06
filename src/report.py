"""Генерация итогового markdown-протокола симуляции прений.

Протокол сохраняется в ``output/verdict_<timestamp>.md``: параметры симуляции
(включая модели и роутер — требование логирования), выжимка материалов дела,
реплики по раундам с авторами и итоговое мотивированное решение судьи.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .graph import DebateResult

logger = logging.getLogger(__name__)


def render_report(result: DebateResult, generated_at: datetime | None = None) -> str:
    """Собрать markdown-протокол из результата симуляции."""
    moment = generated_at or datetime.now()
    cfg, materials = result.cfg, result.materials

    lines: list[str] = [
        "# Протокол судебного симулятора",
        "",
        f"*Сгенерировано: {moment:%d.%m.%Y %H:%M:%S}*",
        "",
        "## Параметры симуляции",
        "",
        f"- **Юрисдикция:** {cfg.jurisdiction}",
        f"- **Роутер:** `{cfg.api_base_url}`",
        f"- **Модель юриста заявителя:** `{cfg.model_claimant_lawyer}`",
        f"- **Модель юриста ответчика:** `{cfg.model_defendant_lawyer}`",
        f"- **Модель судьи:** `{cfg.model_judge}`",
        f"- **Раундов проведено:** {result.rounds_played} "
        f"(лимит max_rounds={cfg.max_rounds}; завершено "
        f"{'решением судьи' if result.finished_by_judge else 'лимитом раундов'})",
        f"- **Документов дела:** {len(materials.fragments)}"
        + (" (материалы суммаризированы LLM из-за лимита контекста)" if materials.summarized else ""),
        f"- **Оценка объёма контекста:** ~{materials.estimated_tokens} токенов",
    ]
    if cfg.llm_params:
        lines.append(f"- **Доп. параметры запросов:** `{cfg.llm_params}`")
    lines.append(f"- **Сторона для рекомендаций:** {result.target_side}")
    if result.legal_warning:
        lines.append(
            f"- **⚠ Предупреждение:** {result.legal_warning} — ссылки модели на нормы "
            "требуют проверки."
        )

    lines += [
        "",
        "## Материалы дела (выжимка)",
        "",
        "### Промпт-контекст (case_context.md)",
        "",
        materials.context.strip(),
        "",
        "### Источники",
        "",
    ]
    lines += [f"- `{f.source}` — {len(f.content)} символов" for f in materials.fragments]

    lines += ["", "## Прения сторон", ""]
    current_round: int | None = None
    for statement in result.history:
        if statement.round != current_round:
            current_round = statement.round
            lines += [f"### Раунд {statement.round}", ""]
        lines += [f"#### {statement.speaker_title}", "", statement.text.strip(), ""]

    lines += ["## Итоговое решение судьи", "", result.verdict.strip(), ""]

    if result.verified_norms:
        lines += ["## Подтверждённые нормы права (pravo.gov.ru)", ""]
        lines += [f"- {norm.title} [{norm.source_url}]" for norm in result.verified_norms]
        lines.append("")

    if result.recommendations is not None:
        rec = result.recommendations
        lines += [
            "---",
            "",
            f"# РЕКОМЕНДАЦИИ ДЛЯ СТОРОНЫ: {rec.side_title.upper()}",
            "",
            f"**Качественная оценка перспектив: {rec.prospects.upper()}**",
            "",
            "⚠ Блок подготовлен ИИ-аналитиком для подготовки к спору и не заменяет юриста.",
            "",
            rec.text.strip(),
            "",
        ]
    return "\n".join(lines)


def save_report(result: DebateResult, output_dir: Path | None = None) -> Path:
    """Сохранить протокол в ``output/verdict_<timestamp>.md`` и вернуть путь."""
    out_dir = Path(output_dir) if output_dir is not None else result.cfg.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"verdict_{stamp}.md"
    path.write_text(render_report(result), encoding="utf-8")
    logger.info("Протокол сохранён: %s", path)
    return path
