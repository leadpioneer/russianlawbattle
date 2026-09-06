"""LegalResearchService — оркестрация провайдеров (шаги 4–5 этапа 3).

Собирает Evidence Pack: healthcheck провайдеров → обращение в порядке priority
(таймаут на каждого) → нормы и практика раздельно → дедупликация →
ранжирование по релевантности и верификации. Возвращает Evidence Pack даже при
частичной недоступности сети: падение провайдера — это статус и warning, а не
исключение. «Первый успешный ответ победителем» не считается: если доступны
несколько провайдеров, сохраняются все (официальный — приоритет при цитировании).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .models import EvidencePack, LegalSource, ProviderHealth, now_iso
from .providers.base import LegalProvider
from .providers.mock import MockLegalProvider
from .providers.pravo_gov import PravoGovProvider

logger = logging.getLogger(__name__)


@dataclass
class ProviderConfig:
    """Настройка одного провайдера (из секции legal_research конфига)."""

    name: str
    enabled: bool = True
    priority: int = 100  # меньше = раньше
    timeout_seconds: float = 20.0


@dataclass
class ResearchResult:
    """Итог исследования: pack + статусы + время."""

    pack: EvidencePack
    provider_statuses: list[ProviderHealth] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def degraded(self) -> bool:
        """Правовое исследование деградировало: нет ни одного verified-источника."""
        return not self.pack.verified_sources


#: Реестр известных провайдеров (расширяется по мере появления).
_PROVIDER_REGISTRY: dict[str, type] = {
    "pravo_gov": PravoGovProvider,
    "mock": MockLegalProvider,
}


def build_providers(configs: list[ProviderConfig]) -> list[tuple[ProviderConfig, LegalProvider]]:
    """Создать экземпляры включённых провайдеров, отсортировать по priority."""
    pairs: list[tuple[ProviderConfig, LegalProvider]] = []
    for cfg in configs:
        if not cfg.enabled:
            continue
        cls = _PROVIDER_REGISTRY.get(cfg.name)
        if cls is None:
            logger.warning("Неизвестный провайдер '%s' — пропущен", cfg.name)
            continue
        pairs.append((cfg, cls()))
    pairs.sort(key=lambda pair: pair[0].priority)
    return pairs


def default_provider_configs() -> list[ProviderConfig]:
    """Дефолтная конфигурация (пока секция legal_research не введена в шаге 6)."""
    return [
        ProviderConfig(name="pravo_gov", enabled=True, priority=100, timeout_seconds=25.0),
    ]


class LegalResearchService:
    """Единая точка правового исследования для графа и API."""

    def __init__(self, provider_configs: list[ProviderConfig] | None = None) -> None:
        self._configs = provider_configs if provider_configs is not None else default_provider_configs()
        self.providers = build_providers(self._configs)

    # -- health -----------------------------------------------------------

    async def healthcheck_all(self) -> list[ProviderHealth]:
        """Параллельный healthcheck всех провайдеров (каждый не бросает)."""
        tasks = [provider.healthcheck() for _, provider in self.providers]
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        statuses: list[ProviderHealth] = []
        for (cfg, provider), result in zip(self.providers, results):
            if isinstance(result, Exception):
                statuses.append(
                    ProviderHealth(
                        provider=provider.name,
                        status="unavailable",
                        transport=None,
                        checked_at=now_iso(),
                        capabilities=[],
                        message=f"{type(result).__name__}: {result}",
                    )
                )
            else:
                statuses.append(result)
        return statuses

    # -- research -----------------------------------------------------------

    async def research(self, queries: list[str], jurisdiction: str, case_id: str = "case") -> ResearchResult:
        """Собрать Evidence Pack по списку запросов. Не бросает исключений."""
        started = time.monotonic()
        statuses = await self.healthcheck_all()
        status_by_name = {s.provider: s for s in statuses}

        all_statutes: list[LegalSource] = []
        all_case_law: list[LegalSource] = []
        warnings: list[str] = []

        for cfg, provider in self.providers:
            healthy = status_by_name.get(provider.name)
            if healthy and healthy.status in ("unavailable", "not_configured"):
                warnings.append(
                    f"провайдер {provider.name} недоступен ({healthy.message or 'без деталей'})"
                )
                continue
            for query in queries:
                try:
                    statutes = await asyncio.wait_for(
                        provider.search_statutes(query, jurisdiction, limit=5),
                        timeout=cfg.timeout_seconds,
                    )
                    all_statutes.extend(statutes)
                except asyncio.TimeoutError:
                    warnings.append(f"провайдер {provider.name}: таймаут поиска «{query[:50]}»")
                except Exception as exc:  # noqa: BLE001 — ошибка провайдера не роняет сборку
                    warnings.append(f"провайдер {provider.name}: {type(exc).__name__}: {exc}")

                try:
                    case_law = await asyncio.wait_for(
                        provider.search_case_law(query, jurisdiction, limit=5),
                        timeout=cfg.timeout_seconds,
                    )
                    all_case_law.extend(case_law)
                except asyncio.TimeoutError:
                    warnings.append(f"провайдер {provider.name}: таймаут поиска практики")
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"провайдер {provider.name} (практика): {type(exc).__name__}: {exc}")

        statutes = self._dedupe(all_statutes)
        case_law = self._dedupe(all_case_law)
        self._renumber(statutes, prefix="LAW")
        self._renumber(case_law, prefix="CASE")

        pack = EvidencePack(
            case_id=case_id,
            jurisdiction=jurisdiction,
            generated_at=now_iso(),
            legal_issues=list(queries),
            sources=[*statutes, *case_law],
            provider_statuses=statuses,
            warnings=self._dedupe_strings(warnings),
        )
        return ResearchResult(
            pack=pack,
            provider_statuses=statuses,
            duration_s=round(time.monotonic() - started, 2),
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _dedupe(sources: list[LegalSource]) -> list[LegalSource]:
        """Дедупликация по URL/названию; лучший экземпляр (по sort_key) побеждает."""
        best: dict[str, LegalSource] = {}
        for source in sources:
            key = (source.official_url or source.title).strip().lower()
            existing = best.get(key)
            if existing is None or LegalResearchService._source_sort_key(source) < LegalResearchService._source_sort_key(existing):
                best[key] = source
        return sorted(best.values(), key=LegalResearchService._source_sort_key)

    @staticmethod
    def _source_sort_key(source: LegalSource) -> tuple:
        """Меньше = лучше: verified → официальный провайдер → релевантность."""
        status_rank = {
            "verified": 0, "partially_verified": 1, "unverified": 2,
            "contradicted": 3, "unavailable": 4,
        }
        provider_rank = 0 if source.provider == "pravo_gov" else 1
        return (status_rank.get(source.verification_status, 3), provider_rank, -source.relevance_score)

    @staticmethod
    def _renumber(sources: list[LegalSource], prefix: str) -> None:
        for i, source in enumerate(sources, 1):
            source.id = f"{prefix}-{i:03d}"

    @staticmethod
    def _dedupe_strings(items: list[str]) -> list[str]:
        seen: set[str] = set()
        result = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result


