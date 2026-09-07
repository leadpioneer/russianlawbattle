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
from .providers.sonar import SonarWebSearchProvider
from .supreme_court import SupremeCourtOfficialProvider
from .case_law import (
    CaseLawCoverage,
    MASS_SOURCES_UNAVAILABLE,
    UnavailableCaseLawProvider,
    user_acts_from_materials,
)

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
    case_law_coverage: CaseLawCoverage | None = None

    @property
    def degraded(self) -> bool:
        """Правовое исследование деградировало: нет ни одного verified-источника."""
        return not self.pack.verified_sources


#: Реестр известных провайдеров (расширяется по мере появления).
_PROVIDER_REGISTRY: dict[str, type] = {
    "pravo_gov": PravoGovProvider,
    "mock": MockLegalProvider,
    "supreme_court_official": SupremeCourtOfficialProvider,
    "case_law_unavailable": UnavailableCaseLawProvider,
    "sonar_web_search": SonarWebSearchProvider,
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


def default_provider_configs(
    legal_research: dict | None = None,
) -> list[ProviderConfig]:
    """Дефолтная конфигурация провайдеров правового исследования.

    :param legal_research: настройки из конфига (config.yaml или сессии):
        ``web_search`` (по умолчанию True), ``search_model``,
        ``fulltext_verify`` (по умолчанию True).
    sonar (веб-поиск) включается флагом ``web_search``.
    """
    settings = legal_research or {}
    configs = [
        ProviderConfig(name="pravo_gov", enabled=True, priority=100, timeout_seconds=25.0),
        ProviderConfig(name="supreme_court_official", enabled=True, priority=90, timeout_seconds=25.0),
    ]
    if settings.get("web_search", True):
        # Веб-поиск через sonar (роутер): дополняет официальные API практикой
        # нижестоящих судов. Результаты — partially_verified (сверка вручную).
        configs.append(
            ProviderConfig(
                name="sonar_web_search", enabled=True, priority=80, timeout_seconds=90.0
            )
        )
    return configs


def _web_search_enabled() -> bool:
    """Флаг ``legal_research.web_search`` из config.yaml (по умолчанию True)."""
    try:
        from ..config import load_config

        return bool(load_config().legal_research.get("web_search", True))
    except Exception:  # noqa: BLE001 — конфиг недоступен (тесты) → дефолт
        return True


class LegalResearchService:
    """Единая точка правового исследования для графа и API."""

    def __init__(
        self,
        provider_configs: list[ProviderConfig] | None = None,
        *,
        legal_research: dict | None = None,
    ) -> None:
        self.legal_research = dict(legal_research or {})
        if provider_configs is not None:
            self._configs = provider_configs
        else:
            self._configs = default_provider_configs(self.legal_research)
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

    async def research(
        self,
        queries: list[str],
        jurisdiction: str,
        case_id: str = "case",
        *,
        materials=None,
    ) -> ResearchResult:
        """Собрать Evidence Pack по списку запросов. Не бросает исключений.

        :param materials: CaseMaterials — если задан, судебные акты из
            загруженных пользователем документов попадают в pack как
            ``case_law``/``user_document`` с уровнем ``USER``.
        """
        started = time.monotonic()
        statuses = await self.healthcheck_all()
        status_by_name = {s.provider: s for s in statuses}

        all_statutes: list[LegalSource] = []
        all_case_law: list[LegalSource] = []
        warnings: list[str] = []

        # Практика из загруженных пользователем документов (шаг 8, п.2).
        user_acts: list[LegalSource] = []
        if materials is not None and getattr(materials, "fragments", None):
            user_acts = user_acts_from_materials(materials.fragments)

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

        # Полная сверка веб-источников по полному тексту (top-K, см. fulltext.py).
        if self.legal_research.get("fulltext_verify", True):
            statutes, case_law = await self._enrich_fulltext(statutes, case_law)

        # Честное покрытие практики (шаг 8, п.4).
        searched = [
            provider.name
            for _, provider in self.providers
            if "case_law" in next(
                (
                    s.capabilities for s in statuses if s.provider == provider.name
                ),
                [],
            )
            and next((s for s in statuses if s.provider == provider.name), None)
            and next((s for s in statuses if s.provider == provider.name)).status
            in ("healthy", "degraded")
        ]
        not_searched = [
            status.provider
            for status in statuses
            if status.provider not in searched
        ]
        coverage = CaseLawCoverage(
            searched_sources=searched,
            not_searched_sources=not_searched,
            coverage="official_only" if searched else "unavailable",
            warning=None if searched else MASS_SOURCES_UNAVAILABLE,
        )
        if not case_law and not user_acts:
            coverage.warning = coverage.user_facing_message()
        elif not case_law:
            coverage.warning = (
                "по подключённым источникам практика не найдена; "
                + coverage.user_facing_message()
            )

        pack = EvidencePack(
            case_id=case_id,
            jurisdiction=jurisdiction,
            generated_at=now_iso(),
            legal_issues=list(queries),
            sources=[*statutes, *case_law, *user_acts],
            provider_statuses=statuses,
            warnings=self._dedupe_strings(warnings),
        )
        pack.case_law_coverage = coverage
        return ResearchResult(
            pack=pack,
            provider_statuses=statuses,
            duration_s=round(time.monotonic() - started, 2),
            case_law_coverage=coverage,
        )

    # -- helpers -----------------------------------------------------------

    #: Сколько источников каждого типа сверять по полному тексту за прогон.
    FULLTEXT_TOP_K = 4

    async def _enrich_fulltext(
        self, statutes: list[LegalSource], case_law: list[LegalSource]
    ) -> tuple[list[LegalSource], list[LegalSource]]:
        """Поднять часть веб-источников до verified через сверку полного текста.

        Только источники sonar (provider=sonar_web_search) с URL, top-K каждого
        типа; каждая сверка в отдельном потоке, ошибка не прерывает сборку.
        """
        import asyncio

        from .fulltext import verify_source_fulltext

        def _upgrade(sources: list[LegalSource]) -> list[LegalSource]:
            candidates = [
                (i, s) for i, s in enumerate(sources)
                if s.provider == "sonar_web_search" and s.official_url
            ][: self.FULLTEXT_TOP_K]
            result = list(sources)
            for i, source in candidates:
                try:
                    result[i] = verify_source_fulltext(source)
                except Exception as exc:  # noqa: BLE001 — никогда не роняем research
                    logger.warning("fulltext: %s: %s", source.id, exc)
            return result

        statutes = await asyncio.to_thread(_upgrade, statutes)
        case_law = await asyncio.to_thread(_upgrade, case_law)
        return statutes, case_law

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


