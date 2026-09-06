"""SonarWebSearchProvider — веб-поиск правовых источников через Perplexity Sonar.

Модель ``perplexity/sonar-pro-search`` (доступна на роутере, [OI]-совместимый
API) выполняет LLM + веб-поиск в одном запросе и отвечает со списком
источников (citations). Провайдер просит её найти действующие НПА РФ и
правоприменительную практику по запросу и превращает ссылки в LegalSource.

Честность результатов (принцип базы): источник, найденный веб-поиском ИИ,
не может быть ``verified`` — максимум ``partially_verified`` с warning
«требует сверки по официальным источникам». Финальную проверку ссылок всё
равно выполняет citation_verifier на узле linter.
"""

from __future__ import annotations

import logging
import os
import re
import time

import httpx

from ..models import LegalSource, ProviderHealth, now_iso
from ...llm_client import TokenUsage

logger = logging.getLogger(__name__)

PROVIDER_NAME = "sonar_web_search"
DEFAULT_SONAR_MODEL = "perplexity/sonar-pro-search"

_HTTP_TIMEOUT_S = 90.0

_SYSTEM_PROMPT = (
    "Ты — ассистент юриста по российскому праву. Ищешь только официальные "
    "источники Российской Федерации: нормативные правовые акты "
    "(pravo.gov.ru, publication.pravo.gov.ru), постановления и обзоры "
    "Верховного Суда РФ (vsrf.ru), практику судов общей юрисдикции и "
    "арбитражных судов. Иностранные источники, блоги, статьи и реклама "
    "запрещены. Отвечай по-русски, кратко и только по существу запроса."
)

_USER_PROMPT = (
    "Запрос: {query}\n\n"
    "Найди {kind} по этому запросу (не более {limit} позиций). Для каждой "
    "позиции приведи отдельной строкой строгого формата:\n"
    "1. <полные реквизиты: акт/постановление, орган, дата, номер> | "
    "<официальный URL источника (pravo.gov.ru, vsrf.ru или сайт суда)> | "
    "<суть в одном предложении>\n"
    "Формат соблюдай точно: три поля через вертикальную черту. Если ничего "
    "подходящего не нашлось — напиши ровно: НИЧЕГО НЕ НАЙДЕНО"
)

#: Строка ответа sonar: «реквизиты | URL | суть».
_RESULT_LINE_RE = re.compile(r"^\s*\d*[.)]?\s*(.+?)\s*\|\s*(\S+)\s*\|\s*(.+?)\s*$")

#: Markdown-ссылки в тексте ответа — резервный источник URL.
_MD_LINK_RE = re.compile(r"\[([^\]]{5,200})\]\((https?://[^)\s]+)\)")

#: «Голые» URL в прозе ответа — последний резерв.
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"']{8,}")

_ALLOWED_URL_HOSTS = (
    "pravo.gov.ru", "publication.pravo.gov.ru", "vsrf.ru", "www.vsrf.ru",
    "sudact.ru", "kad.arbitr.ru", "sudrf.ru", "supcourt.ru",
)


def parse_sonar_answer(answer: str, query: str) -> list[dict]:
    """Разобрать ответ sonar в список словарей {title, url, excerpt}.

    Сначала строгий формат «1. реквизиты | URL | суть», при пустом результате —
    markdown-ссылки из текста. Ничего похожего на источники → [].
    """
    results: list[dict] = []
    seen_urls: set[str] = set()

    for line in _iter_candidate_lines(answer):
        match = _RESULT_LINE_RE.match(line)
        if not match:
            continue
        title, url, excerpt = match.group(1).strip(), match.group(2).strip(), match.group(3).strip()
        if not _url_allowed(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append({"title": title[:200], "url": url, "excerpt": excerpt[:400]})

    if results:
        return results

    # Резерв 1: markdown-ссылки с белым списком доменов.
    for title, url in _MD_LINK_RE.findall(answer):
        if not _url_allowed(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append({"title": title.strip()[:200], "url": url, "excerpt": ""})
    if results:
        return results

    # Резерв 2: «голые» URL официальных доменов в прозе — берём предложение
    # вокруг ссылки как заголовок (sonar иногда отвечает не по формату).
    for match in _BARE_URL_RE.finditer(answer):
        url = match.group(0).rstrip(".,;:)")
        if not _url_allowed(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        sentence_start = answer.rfind(".", 0, match.start())
        sentence_end = answer.find(".", match.end())
        sentence = answer[sentence_start + 1 : sentence_end if sentence_end > 0 else None]
        title = sentence.strip().strip("«»*") or url
        results.append({"title": title[:200], "url": url, "excerpt": ""})
    return results


def _iter_candidate_lines(answer: str):
    yield from (line.strip() for line in answer.splitlines())


def _url_allowed(url: str) -> bool:
    if not url.lower().startswith(("http://", "https://")):
        return False
    host = url.split("/")[2].lower() if "://" in url else ""
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_URL_HOSTS)



class SonarWebSearchProvider:
    """Поиск НПА и практики через sonar на роутере ([OI]-совместимый API)."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        post: object | None = None,
    ) -> None:
        self._base_url = base_url  # лениво из конфига (иначе тесты не подменить)
        self._api_key = api_key
        # Приоритет алиаса: явный аргумент > config.yaml (search_model) >
        # env SONAR_MODEL > дефолт.
        self._model = model or self._model_from_config() or os.getenv(
            "SONAR_MODEL", DEFAULT_SONAR_MODEL
        )
        self._post = post  # хук для тестов: Callable[[str, str], tuple[str, TokenUsage]]

    @staticmethod
    def _model_from_config() -> str | None:
        """Алиас поисковой модели из config.yaml (legal_research.search_model)."""
        try:
            from ...config import load_config

            model = load_config().legal_research.get("search_model")
            return model.strip() if isinstance(model, str) and model.strip() else None
        except Exception:  # noqa: BLE001 — конфиг опционален (тесты без config.yaml)
            return None

    def _credentials(self) -> tuple[str, str]:
        if self._base_url is not None and self._api_key is not None:
            return self._base_url, self._api_key
        from ...config import load_config

        cfg = load_config()
        return cfg.api_base_url, cfg.api_key

    def _ask(self, query: str, kind: str, limit: int) -> tuple[str, TokenUsage]:
        """Один запрос к sonar; возвращает (текст, usage). Бросает исключения."""
        if self._post is not None:
            return self._post(query, kind)  # type: ignore[misc]

        base_url, api_key = self._credentials()
        url = f"{base_url.rstrip('/')}/chat/completions"
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": _USER_PROMPT.format(query=query, kind=kind, limit=limit),
                    },
                ],
                "temperature": 0.2,
            },
            timeout=_HTTP_TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json()
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = str(message.get("content") or "").strip()
        usage = TokenUsage.from_api(payload.get("usage") or {})
        return text, usage

    def _search(
        self, query: str, kind: str, *, source_type: str, authority: str, limit: int
    ) -> list[LegalSource]:
        q = (query or "").strip()
        if not q:
            return []  # пустой запрос — честный пустой ответ (без фантазий)
        started = time.monotonic()
        try:
            text, usage = self._ask(q, kind, limit)
        except Exception as exc:  # noqa: BLE001 — ошибка поиска → пусто, не падение
            logger.warning("%s: запрос не удался: %s", PROVIDER_NAME, exc)
            return []

        from ...llm_client import log_external_usage

        log_external_usage("legal_research", self._model, usage)
        logger.info(
            "%s: %s за %.1fс, токены %s, символов %d",
            PROVIDER_NAME,
            kind,
            time.monotonic() - started,
            f"{usage.input_tokens}/{usage.output_tokens}" if usage.total_tokens else "нет",
            len(text),
        )

        sources: list[LegalSource] = []
        for index, item in enumerate(parse_sonar_answer(text, q)[:limit], start=1):
            sources.append(
                LegalSource(
                    id=f"WEB-{index:03d}",
                    source_type=source_type,
                    title=item["title"],
                    authority=authority,
                    citation=item["title"][:200],
                    excerpt=item["excerpt"],
                    official_url=item["url"],
                    verified=False,
                    verification_status="partially_verified",
                    provider=PROVIDER_NAME,
                    retrieved_at=now_iso(),
                    relevance_score=5.0,
                    supports_issues=[q[:100]],
                    authority_level="B",
                    warning=(
                        "источник найден веб-поиском ИИ; сверьте реквизиты "
                        "и текст по официальному источнику"
                    ),
                )
            )
        return sources

    async def healthcheck(self) -> ProviderHealth:
        import asyncio

        checked_at = now_iso()
        base_url, _ = self._credentials()

        def _probe() -> tuple[str, TokenUsage]:
            return self._ask(
                "действующая редакция статьи 309 ГК РФ",
                "нормативные правовые акты",
                limit=1,
            )

        try:
            text, _usage = await asyncio.to_thread(_probe)
        except Exception as exc:  # noqa: BLE001 — healthcheck не бросает
            return ProviderHealth(
                provider=self.name,
                status="unavailable",
                transport="web_search",
                checked_at=checked_at,
                capabilities=[],
                message=f"{type(exc).__name__}: {exc}",
            )
        return ProviderHealth(
            provider=self.name,
            status="healthy" if text else "degraded",
            transport="web_search",
            checked_at=checked_at,
            capabilities=["statutes", "case_law"],
            message=f"sonar доступен ({base_url}); результаты требуют сверки",
        )

    async def search_statutes(
        self, query: str, jurisdiction: str, limit: int = 8
    ) -> list[LegalSource]:
        import asyncio

        return await asyncio.to_thread(
            self._search,
            query,
            "действующие нормативные правовые акты РФ (закон, статья, редакция)",
            source_type="statute",
            authority="Российская Федерация",
            limit=limit,
        )

    async def get_document(self, source_id: str) -> LegalSource | None:
        return None

    async def search_case_law(
        self, query: str, jurisdiction: str, limit: int = 8
    ) -> list[LegalSource]:
        import asyncio

        return await asyncio.to_thread(
            self._search,
            query,
            "правоприменительную практику: постановления Пленума и Президиума "
            "Верховного Суда РФ, обзоры практики, решения судов по аналогичным делам",
            source_type="case_law",
            authority="Суды Российской Федерации",
            limit=limit,
        )

