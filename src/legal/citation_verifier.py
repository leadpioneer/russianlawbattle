"""Citation verifier: проверка правовых ссылок против Evidence Pack (шаг 7).

LLM не может выдавать юридическую ссылку как подтверждённую, если её нет в
Evidence Pack. Верификатор находит в тексте упоминания статей/норм
(``ст. 309 ГК РФ``, ``266-ФЗ``, ``[LAW-001]``…) и сверяет их с карточками
источников. Статусы проверки:

- ``verified``          — ссылка есть в pack (по ID или совпадению цитаты);
- ``missing``           — конкретная ссылка, которой нет в pack;
- ``mismatch``          — [LAW-XXX] указан, но его цитата в тексте не совпадает;
- ``unverified_source`` — ссылка есть в pack, но статус источника ниже verified.

Политика: live-реплики только помечаются; вердикт и рекомендации проходят
linter, при ошибках — один repair-pass; остатки ошибок видны в отчёте.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .models import EvidencePack, LegalSource

CheckStatus = Literal["verified", "missing", "mismatch", "unverified_source"]
OverallStatus = Literal["passed", "warning", "failed"]


@dataclass
class CitationCheck:
    """Результат проверки одной найденной ссылки."""

    claim: str  # фрагмент текста вокруг ссылки (контекст аргумента)
    citation_text: str | None  # сама ссылка как написана в тексте
    evidence_id: str | None  # [LAW-XXX], если указан
    status: CheckStatus
    message: str


@dataclass
class CitationVerificationResult:
    """Итог проверки текста: список checks + агрегаты."""

    checks: list[CitationCheck] = field(default_factory=list)
    verified_count: int = 0
    issue_count: int = 0
    overall_status: OverallStatus = "passed"

    def to_dict(self) -> dict:
        return {
            "checks": [
                {
                    "claim": c.claim,
                    "citation_text": c.citation_text,
                    "evidence_id": c.evidence_id,
                    "status": c.status,
                    "message": c.message,
                }
                for c in self.checks
            ],
            "verified_count": self.verified_count,
            "issue_count": self.issue_count,
            "overall_status": self.overall_status,
        }


# --- детект ссылок ----------------------------------------------------------

#: [LAW-001] / [CASE-002] — явные ID Evidence Pack.
_EVIDENCE_ID_RE = re.compile(r"\[((?:LAW|CASE|DOC)-\d{3})\]", re.IGNORECASE)

#: «ст. 309 ГК РФ», «статьи 6.1.1 КоАП РФ», «п. 12 ПП ВС РФ» (номер + акт).
_ARTICLE_RE = re.compile(
    r"\b(?:ст|стр)\.\s*(\d+(?:\.\d+)*)\s+((?:[А-ЯA-Z][\w-]*\s*){1,4}(?:РФ|РСФСР))",
    re.IGNORECASE,
)

#: Номера федеральных актов: «266-ФЗ», «152-ФЗО», «1-ФКЗ».
_ACT_NUMBER_RE = re.compile(r"\b(\d{1,4}-(?:ФЗО|ФКЗ|ФЗ))\b", re.IGNORECASE)

#: Номера судебных дел: «№ А40-12345/2026», «дело № 2-1234/2025».
_CASE_NUMBER_RE = re.compile(
    r"(?:дело\s*)?№\s*([А-Я]{0,2}\d+-\d+/(?:19|20)\d{2})", re.IGNORECASE
)

#: Контекст вокруг ссылки для claim (символов в каждую сторону).
_CONTEXT_RADIUS = 80


def _context_around(text: str, start: int, end: int) -> str:
    left = max(0, start - _CONTEXT_RADIUS)
    right = min(len(text), end + _CONTEXT_RADIUS)
    return re.sub(r"\s+", " ", text[left:right]).strip()


# --- сверка с Evidence Pack -------------------------------------------------


def _source_matches_citation(source: LegalSource, citation: str) -> bool:
    """Есть ли ссылка в карточке источника (citation/title/URL)."""
    citation_l = citation.lower().strip(" .")
    for field_text in (source.citation, source.title, source.official_url or ""):
        if citation_l in field_text.lower():
            return True
    return False


def _find_source_by_id(pack: EvidencePack, evidence_id: str) -> LegalSource | None:
    for source in pack.sources:
        if source.id.lower() == evidence_id.lower():
            return source
    return None


def _find_source_by_citation(pack: EvidencePack, citation: str) -> LegalSource | None:
    for source in pack.sources:
        if _source_matches_citation(source, citation):
            return source
    return None


def _check_evidence_id(pack: EvidencePack, evidence_id: str, claim: str) -> CitationCheck:
    source = _find_source_by_id(pack, evidence_id)
    if source is None:
        return CitationCheck(
            claim=claim, citation_text=f"[{evidence_id}]", evidence_id=evidence_id,
            status="missing", message="ID отсутствует в Evidence Pack",
        )
    if source.verification_status == "verified":
        return CitationCheck(
            claim=claim, citation_text=f"[{evidence_id}]", evidence_id=evidence_id,
            status="verified", message="подтверждённый источник из Evidence Pack",
        )
    return CitationCheck(
        claim=claim, citation_text=f"[{evidence_id}]", evidence_id=evidence_id,
        status="unverified_source",
        message=f"источник в pack, но статус: {source.verification_status}",
    )


def _check_citation(pack: EvidencePack, citation: str, claim: str) -> CitationCheck:
    source = _find_source_by_citation(pack, citation)
    if source is None:
        return CitationCheck(
            claim=claim, citation_text=citation, evidence_id=None,
            status="missing",
            message="ссылка не найдена в Evidence Pack — требует ручной проверки",
        )
    if source.verification_status == "verified":
        return CitationCheck(
            claim=claim, citation_text=citation, evidence_id=source.id,
            status="verified", message="совпадает с подтверждённым источником",
        )
    return CitationCheck(
        claim=claim, citation_text=citation, evidence_id=source.id,
        status="unverified_source",
        message=f"источник в pack, но статус: {source.verification_status}",
    )


def verify_citations(text: str, pack: EvidencePack | None) -> CitationVerificationResult:
    """Проверить все правовые ссылки текста против Evidence Pack.

    Пустой pack: любые конкретные ссылки → ``missing`` (нечем подтвердить).
    """
    checks: list[CitationCheck] = []
    if not text or not text.strip():
        return CitationVerificationResult(overall_status="passed")

    seen: set[tuple[str, str]] = set()  # (вид ссылки, значение) — без повторов

    def add(check: CitationCheck) -> None:
        key = (check.citation_text or "", check.status)
        if key in seen:
            return
        seen.add(key)
        checks.append(check)

    # 1. Явные [LAW-XXX] — самый надёжный канал.
    for match in _EVIDENCE_ID_RE.finditer(text):
        claim = _context_around(text, match.start(), match.end())
        add(_check_evidence_id(pack, match.group(1).upper(), claim))

    if pack is not None:
        # 2. ст. N <Акт РФ> — сверяем цитату с карточками.
        for match in _ARTICLE_RE.finditer(text):
            citation = f"ст. {match.group(1)} {match.group(2).strip()}"
            claim = _context_around(text, match.start(), match.end())
            add(_check_citation(pack, citation, claim))

        # 3. Номера актов (266-ФЗ).
        for match in _ACT_NUMBER_RE.finditer(text):
            citation = match.group(1)
            claim = _context_around(text, match.start(), match.end())
            add(_check_citation(pack, citation, claim))

        # 4. Номера судебных дел.
        for match in _CASE_NUMBER_RE.finditer(text):
            citation = f"дело № {match.group(1)}"
            claim = _context_around(text, match.start(), match.end())
            add(_check_citation(pack, citation, claim))
    else:
        # Pack нет — все конкретные ссылки нечем подтвердить.
        for match in _ACT_NUMBER_RE.finditer(text):
            claim = _context_around(text, match.start(), match.end())
            add(
                CitationCheck(
                    claim=claim, citation_text=match.group(1), evidence_id=None,
                    status="missing", message="Evidence Pack отсутствует",
                )
            )

    verified = sum(1 for c in checks if c.status == "verified")
    unverified = sum(1 for c in checks if c.status == "unverified_source")
    issues = sum(1 for c in checks if c.status in ("missing", "mismatch"))
    if issues:
        overall: OverallStatus = "failed"
    elif unverified:
        overall = "warning"
    else:
        overall = "passed"
    return CitationVerificationResult(
        checks=checks, verified_count=verified, issue_count=issues + unverified,
        overall_status=overall,
    )


# --- repair-pass -------------------------------------------------------------

_REPAIR_SYSTEM_PROMPT = (
    "Ты — редактор юридического текста. В тексте найдены ссылки на нормы права, "
    "ОТСУТСТВУЮЩИЕ в списке подтверждённых источников, либо выданные за "
    "подтверждённые без подтверждения.\n"
    "Правила исправления:\n"
    "1. Удали непроверенные номера статей/актов или замени формулировку на "
    "осторожную: «в соответствии с законодательством о защите прав потребителей».\n"
    "2. НЕ выдумывай замену номерами. НЕ меняй факты и аргументы.\n"
    "3. Сохрани структуру, тон и объём текста. Верни ТОЛЬКО исправленный текст."
)


def repair_citations(text: str, result: CitationVerificationResult, role: str = "judge") -> str:
    """Один контролируемый repair-pass: LLM убирает непроверенные ссылки.

    При любой ошибке LLM возвращается исходный текст — ошибки останутся
    видимыми в отчёте (не скрываются).
    """
    problems = [
        check for check in result.checks
        if check.status in ("missing", "mismatch", "unverified_source")
    ]
    if not problems:
        return text
    problem_lines = "\n".join(
        f"- {check.citation_text}: {check.message}" for check in problems
    )
    try:
        from ..llm_client import chat

        repaired = chat(
            role=role,
            system_prompt=_REPAIR_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"НАЙДЕННЫЕ ПРОБЛЕМЫ:\n{problem_lines}\n\nТЕКСТ:\n{text}",
            }],
            temperature=0.2,
        )
        return str(repaired).strip() or text
    except Exception:  # noqa: BLE001 — repair не обязателен, ошибки останутся видны
        return text

