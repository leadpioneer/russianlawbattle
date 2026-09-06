"""Сборка Evidence Pack: экстракция вопросов → research → pack (шаг 5).

Точка входа :func:`build_evidence_pack` — синхронная обёртка над асинхронным
сервисом (граф LangGraph работает в отдельном потоке, без event loop).

Evidence Pack сохраняется в каталог сессии (``evidence_pack.json``) и включается
в JSON-отчёт — пользователь видит происхождение каждого источника.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .issue_extractor import LegalIssues, extract_issues
from .models import EvidencePack
from .service import LegalResearchService, ResearchResult

logger = logging.getLogger(__name__)


async def build_evidence_pack_async(
    context: str,
    materials_summary: str,
    jurisdiction: str,
    *,
    case_id: str = "case",
    free_text_query: str | None = None,
    use_llm: bool = True,
    service: LegalResearchService | None = None,
    materials=None,
) -> tuple[EvidencePack, LegalIssues, ResearchResult]:
    """Async-сборка Evidence Pack (вызывать из async-контекста или обёртки ниже)."""
    service = service or LegalResearchService()

    issues = extract_issues(
        context,
        materials_summary,
        jurisdiction,
        free_text_query=free_text_query,
        use_llm=use_llm,
    )
    queries = issues.search_queries()
    if not queries:
        queries = [materials_summary[:200] or context[:200] or jurisdiction]

    result = await service.research(
        queries, jurisdiction, case_id=case_id, materials=materials
    )

    pack = result.pack
    pack.legal_issues = list(queries)
    # Если конкретные запросы ничего не дали — добираем широким запросом
    # (пустая строка = «без фильтра»: mock отдаёт все фикстуры; реальные
    # провайдеры просто вернут [] и останется degraded-режим).
    if not pack.sources:
        broad = await service.research([""], jurisdiction, case_id=case_id)
        if broad.pack.sources:
            pack = broad.pack
            result = broad
    if result.degraded:
        pack.warnings.append(
            "правовое исследование деградировало: ни одного подтверждённого "
            "источника не найдено; нормы требуют ручной проверки"
        )
    logger.info(
        "Evidence Pack собран за %.1f с: %d источников (verified: %d), warnings: %d",
        result.duration_s,
        len(pack.sources),
        len(pack.verified_sources),
        len(pack.warnings),
    )
    return pack, issues, result


def build_evidence_pack(
    context: str,
    materials_summary: str,
    jurisdiction: str,
    *,
    case_id: str = "case",
    free_text_query: str | None = None,
    use_llm: bool = True,
    service: LegalResearchService | None = None,
    materials=None,
) -> tuple[EvidencePack, LegalIssues, ResearchResult]:
    """Синхронная обёртка для графа/CLI (отдельный поток, без event loop)."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "build_evidence_pack нельзя вызывать внутри event loop — "
            "используйте build_evidence_pack_async"
        )
    return asyncio.run(
        build_evidence_pack_async(
            context,
            materials_summary,
            jurisdiction,
            case_id=case_id,
            free_text_query=free_text_query,
            use_llm=use_llm,
            service=service,
            materials=materials,
        )
    )


def save_evidence_pack(pack: EvidencePack, session_dir: str | Path) -> Path:
    """Сохранить evidence_pack.json в каталог сессии; путь — для отчёта/API."""
    path = Path(session_dir) / "evidence_pack.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pack.to_json(), encoding="utf-8")
    return path


def load_evidence_pack(session_dir: str | Path) -> EvidencePack | None:
    """Прочитать pack из сессии (для /api/.../evidence и отчётов)."""
    path = Path(session_dir) / "evidence_pack.json"
    if not path.exists():
        return None
    try:
        return EvidencePack.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001 — битый файл не роняет отчёт
        logger.warning("evidence_pack.json повреждён: %s", exc)
        return None
