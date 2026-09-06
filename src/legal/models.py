"""Доменные модели правового research layer (шаг 2 этапа 3).

Подход: dataclass + явная сериализация (pydantic в проекте не используется —
держим единый стиль). Ключевой инвариант: ``verified=True`` допустим только при
``verification_status="verified"`` — типы не позволяют выдать непроверенный
источник за подтверждённый (проверяется в ``__post_init__``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Literal

SourceType = Literal[
    "statute",
    "supreme_court",
    "constitutional_court",
    "arbitration_case",
    "general_jurisdiction_case",
    "official_explanation",
    "user_document",
]

VerificationStatus = Literal[
    "verified",
    "partially_verified",
    "unverified",
    "unavailable",
    "contradicted",
]

ProviderStatus = Literal["healthy", "degraded", "unavailable", "not_configured"]


@dataclass
class LegalSource:
    """Единица правового источника: НПА, судебный акт или документ дела."""

    id: str  # LAW-001, CASE-001, DOC-001
    source_type: SourceType
    title: str  # наименование акта/судебного акта/документа
    authority: str  # ГК РФ, Верховный Суд РФ, АС города ...
    citation: str  # ст. 309 ГК РФ, п. 12 ПП ВС РФ № ...
    excerpt: str  # точная выдержка, не пересказ LLM ("" если источник не отдал текст)
    official_url: str | None = None
    effective_date: str | None = None  # для НПА, если источник отдаёт дату
    decision_date: str | None = None  # для судебного акта
    case_number: str | None = None
    court: str | None = None
    verified: bool = False
    verification_status: VerificationStatus = "unverified"
    provider: str = "manual"  # pravo_gov, pravo_mcp, ru_legal_mcp, manual, user_document
    retrieved_at: str = ""
    relevance_score: float = 0.0
    supports_issues: list[str] = field(default_factory=list)
    warning: str | None = None
    #: уровень авторитетности практики (шаг 8): A (КС/Пленум/обзор ВС), B, C, D
    authority_level: str | None = None

    def __post_init__(self) -> None:
        if self.verified and self.verification_status != "verified":
            raise ValueError(
                f"{self.id}: verified=True допустим только при "
                f"verification_status='verified' (получено '{self.verification_status}')"
            )
        if self.verification_status == "verified" and not self.excerpt:
            # «Подтверждённый» без точной выдержки противоречит правилу качества.
            raise ValueError(
                f"{self.id}: verification_status='verified' требует непустой excerpt"
            )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LegalSource":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ProviderHealth:
    """Состояние провайдера на момент проверки."""

    provider: str
    status: ProviderStatus
    transport: str | None  # stdio, http, sse, direct_api, cache
    checked_at: str
    capabilities: list[str] = field(default_factory=list)  # statutes, case_law, documents
    message: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvidencePack:
    """Сводка проверяемых источников дела: на неё опираются все три роли."""

    case_id: str
    jurisdiction: str
    generated_at: str
    legal_issues: list[str] = field(default_factory=list)
    sources: list[LegalSource] = field(default_factory=list)
    provider_statuses: list[ProviderHealth] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -- агрегаты ------------------------------------------------------------

    @property
    def verified_sources(self) -> list[LegalSource]:
        return [s for s in self.sources if s.verification_status == "verified"]

    @property
    def partially_verified_sources(self) -> list[LegalSource]:
        return [s for s in self.sources if s.verification_status == "partially_verified"]

    @property
    def unverified_sources(self) -> list[LegalSource]:
        return [
            s for s in self.sources
            if s.verification_status in ("unverified", "unavailable", "contradicted")
        ]

    def sources_by_type(self, source_type: SourceType) -> list[LegalSource]:
        return [s for s in self.sources if s.source_type == source_type]

    # -- сериализация ---------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "jurisdiction": self.jurisdiction,
            "generated_at": self.generated_at,
            "legal_issues": list(self.legal_issues),
            "sources": [s.to_dict() for s in self.sources],
            "provider_statuses": [p.to_dict() for p in self.provider_statuses],
            "warnings": list(self.warnings),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "EvidencePack":
        return cls(
            case_id=data["case_id"],
            jurisdiction=data["jurisdiction"],
            generated_at=data["generated_at"],
            legal_issues=list(data.get("legal_issues", [])),
            sources=[LegalSource.from_dict(s) for s in data.get("sources", [])],
            provider_statuses=[ProviderHealth(**p) for p in data.get("provider_statuses", [])],
            warnings=list(data.get("warnings", [])),
        )


def make_id(prefix: str, seq: int) -> str:
    """LAW-001 / CASE-001 / DOC-001 — единый формат идентификаторов."""
    return f"{prefix}-{seq:03d}"


def now_iso() -> str:
    """Текущее время в ISO-формате (для retrieved_at/checked_at/generated_at)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

