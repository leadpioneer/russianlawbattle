"""Форматирование Evidence Pack для промптов агентов (шаг 5).

Строгие секции: подтверждённые / частично проверенные / непроверенные —
агенты обязаны видеть статус каждого источника и следовать правилам
цитирования. Компактно: без выдержок verified-источники показываются целиком,
остальные — только реквизиты + причина.
"""

from __future__ import annotations

from .models import EvidencePack, LegalSource

_RULES_VERIFIED = (
    "ПРАВИЛА ЦИТИРОВАНИЯ:\n"
    "1. Точные нормы (статья, пункт, номер акта) можно использовать ТОЛЬКО из "
    "списка подтверждённых источников ниже или из материалов дела — с пометкой [LAW-XXX]/[CASE-XXX].\n"
    "2. Частично проверенные источники упоминать только с оговоркой «по данным, требующим проверки».\n"
    "3. Непроверенные источники НЕЛЬЗЯ выдавать как точные правовые ссылки.\n"
    "4. Любую норму вне списков помечать «(требует проверки)». Номера статей не выдумывать.\n"
    "5. Судебную практику (CASE-*) нельзя называть нормой права: только "
    "«имеется сходная практика», «может поддерживать аргумент»."
)


def _format_source(source: LegalSource) -> str:
    """Одна строка источника: [ID] цитата — название (дата)."""
    parts = [f"[{source.id}] {source.citation}"]
    if source.title and source.title != source.citation:
        parts.append(f"«{source.title[:120]}»")
    if source.effective_date:
        parts.append(f"от {source.effective_date}")
    if source.official_url:
        parts.append(source.official_url)
    return " — ".join(parts)


def _format_excerpt(source: LegalSource, max_len: int = 1200) -> str:
    """Точная выдержка confirmed-источника (текст пришёл из внешнего источника)."""
    text = source.excerpt.strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def format_evidence_block(pack: EvidencePack) -> str:
    """Полный блок Evidence Pack для системного промпта агента.

    Пустой pack → заглушка с явным запретом точных ссылок (degraded-режим).
    """
    if not pack.sources:
        return (
            "ПРАВОВЫЕ ИСТОЧНИКИ: внешние источники недоступны или ничего не найдено.\n"
            + _RULES_VERIFIED
            + "\n6. Точных норм сейчас НЕТ: аргументируйте фактами из материалов дела, "
            "ссылки на нормы формулируйте без номеров и помечайте «(требует проверки)»."
        )

    lines: list[str] = ["ПРОВЕРЕННЫЕ ИСТОЧНИКИ (можно использовать как правовые ссылки):"]
    verified = pack.verified_sources
    if not verified:
        lines.append("  (подтверждённых источников нет)")

    partial: list[LegalSource] = []
    unverified: list[LegalSource] = []
    case_law: list[LegalSource] = []
    for source in pack.sources:
        if source.verification_status == "verified" and source.source_type == "statute":
            lines.append(f"  {_format_source(source)}")
            if source.excerpt:
                lines.append(f"      Текст: {_format_excerpt(source)}")
        elif source.source_type in ("case_law", "supreme_court", "official_explanation"):
            case_law.append(source)
        elif source.verification_status == "partially_verified":
            partial.append(source)
        else:
            unverified.append(source)

    if partial:
        lines.append("")
        lines.append("ЧАСТИЧНО ПРОВЕРЕННЫЕ ИСТОЧНИКИ (упоминать только с оговоркой):")
        lines.extend(f"  {_format_source(s)}" for s in partial)

    if case_law:
        lines.append("")
        lines.append(
            "СУДЕБНАЯ ПРАКТИКА И РАЗЪЯСНЕНИЯ (это НЕ нормы права; формулировать "
            "вероятностно: «имеется сходная практика», «может поддерживать аргумент», "
            "«требуется проверить фактическое сходство»):"
        )
        for source in case_law:
            level = source.authority_level or "USER"
            line = f"  [{source.id}] ({level}) {source.citation} — {source.authority}"
            if source.case_number:
                line += f", дело/акт {source.case_number}"
            if source.decision_date:
                line += f", от {source.decision_date}"
            if source.official_url:
                line += f" — {source.official_url}"
            lines.append(line)
            if source.excerpt and source.verification_status == "verified":
                lines.append(f"      Выдержка: {_format_excerpt(source, 600)}")

    if unverified:
        lines.append("")
        lines.append("НЕПРОВЕРЕННЫЕ ИСТОЧНИКИ (не использовать как точную правовую ссылку):")
        lines.extend(f"  {_format_source(s)}" for s in unverified)

    lines.append("")
    lines.append(_RULES_VERIFIED)

    # Честное покрытие практики (шаг 8).
    coverage = pack.case_law_coverage
    if coverage is not None:
        lines.append("")
        lines.append(f"ПОКРЫТИЕ ПОИСКА ПРАКТИКИ: {coverage.user_facing_message()}")
    return "\n".join(lines)


def format_provider_status_block(pack: EvidencePack) -> str:
    """Компактный блок статусов провайдеров (для отчёта и UI)."""
    if not pack.provider_statuses:
        return "Провайдеры не настроены."
    lines = []
    for status in pack.provider_statuses:
        caps = ", ".join(status.capabilities) or "нет"
        lines.append(f"{status.provider}: {status.status} ({status.transport}); возможности: {caps}")
        if status.message:
            lines.append(f"  {status.message}")
    return "\n".join(lines)
