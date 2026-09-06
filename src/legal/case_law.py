"""Case-law слой: судебная практика — интерфейсы, ВС РФ, user-документы (шаг 8).

Честный MVP (по ТЗ этапа 3):

- **массового программного источника практики нижестоящих судов НЕТ**:
  sudact.ru требует JavaScript, kad.arbitr — капча. Поэтому по умолчанию
  практику ищет только официальный сайт ВС РФ (vsrf.ru): постановления
  Пленума, обзоры практики и опубликованные документы;
- акты, загруженные пользователем в материалы дела, классифицируются как
  ``case_law``/``user_document`` с уровнем ``USER`` — они НЕ считаются внешне
  подтверждёнными, пока реквизиты не сверены с официальным источником;
- формулировка «практика не найдена» допустима только после успешного поиска
  по подключённому источнику; иначе — «массовый поиск по практике
  нижестоящих судов не выполнялся» (см. :class:`CaseLawCoverage`).

Уровни авторитетности (:data:`AUTHORITY_LEVELS`): A — КС РФ/Пленум ВС/обзор ВС,
B — ВС РФ/кассация, C — апелляция, D — первая инстанция, USER — акт загружен
пользователем без внешнего подтверждения.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .models import LegalSource, ProviderHealth, now_iso

AuthorityLevel = Literal["A", "B", "C", "D", "USER"]

AUTHORITY_LEVELS: tuple[AuthorityLevel, ...] = ("A", "B", "C", "D", "USER")

#: Человекочитаемое описание уровней (для UI/отчёта).
AUTHORITY_LABELS: dict[str, str] = {
    "A": "КС РФ, Пленум ВС РФ, обзор практики ВС РФ",
    "B": "ВС РФ (кроме Пленума), кассационные суды",
    "C": "апелляционные суды",
    "D": "первая инстанция",
    "USER": "акт загружен пользователем; реквизиты внешне не подтверждены",
}

#: Что покрывает vsrf.ru-провайдер (для честного coverage).
OFFICIAL_VSRF_SCOPE = (
    "постановления Пленума ВС РФ, обзоры судебной практики ВС РФ и "
    "опубликованные документы на официальном сайте vsrf.ru"
)


@dataclass
class CaseLawCoverage:
    """Честное покрытие поиска практики (в Evidence Pack и отчёте)."""

    searched_sources: list[str] = field(default_factory=list)
    not_searched_sources: list[str] = field(default_factory=list)
    coverage: Literal["official_only", "limited", "unavailable"] = "unavailable"
    warning: str | None = None

    def to_dict(self) -> dict:
        return {
            "searched_sources": list(self.searched_sources),
            "not_searched_sources": list(self.not_searched_sources),
            "coverage": self.coverage,
            "warning": self.warning,
        }

    def user_facing_message(self) -> str:
        """Формулировка для отчёта: «не найдено» только после успешного поиска."""
        if self.searched_sources:
            return (
                f"Поиск выполнен по подключённым источникам ({', '.join(self.searched_sources)}); "
                "по практике нижестоящих судов массовый поиск не выполнялся."
            )
        return (
            "Массовый поиск по судебной практике не выполнялся: подключённых "
            "источников практики нет (ниже — причины по каждому)."
        )


#: Причина недоступности массовых источников практики (единая формулировка).
MASS_SOURCES_UNAVAILABLE = (
    "массовый программный источник практики не подключён: sudact.ru требует "
    "JavaScript, kad.arbitr защищён капчей; автоматический доступ к ним не предусмотрен"
)


class UnavailableCaseLawProvider:
    """Честный провайдер-заглушка: практики не ищет, об этом сообщает.

    Возвращает пустой список и ProviderHealth со статусом ``unavailable`` —
    никакого выдуманного контента.
    """

    name = "case_law_unavailable"

    async def healthcheck(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            status="unavailable",
            transport=None,
            checked_at=now_iso(),
            capabilities=[],
            message=MASS_SOURCES_UNAVAILABLE,
        )

    async def search_statutes(self, query: str, jurisdiction: str, limit: int = 8) -> list[LegalSource]:
        return []

    async def get_document(self, source_id: str) -> LegalSource | None:
        return None

    async def search_case_law(self, query: str, jurisdiction: str, limit: int = 8) -> list[LegalSource]:
        return []


# --- точки расширения (НЕ включены: нет ключа/доступа, не тестировались) -----


class AtomnoCaseLawProvider:
    """DISABLED-заготовка провайдера practice.atomno (без реализации).

    Активация запрещена до появления официального API и ключа доступа;
    healthcheck всегда ``not_configured``. Никаких выдуманных ответов.
    """

    name = "atomno"

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "AtomnoCaseLawProvider — точка расширения без реализации: "
            "нужен официальный API и ключ доступа"
        )


class KadArbitrProvider:
    """DISABLED-заготовка провайдера kad.arbitr (без реализации).

    kad.arbitr защищён капчей; программный доступ не предусмотрен. Активация
    запрещена до появления официального API. Никаких выдуманных ответов.
    """

    name = "kad_arbitr"

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "KadArbitrProvider — точка расширения без реализации: "
            "у kad.arbitr нет открытого API (капча)"
        )


def not_configured_health(name: str) -> ProviderHealth:
    """Стандартный статус для disabled-провайдеров-расширений."""
    return ProviderHealth(
        provider=name,
        status="not_configured",
        transport=None,
        checked_at=now_iso(),
        capabilities=[],
        message="точка расширения не активирована: нет ключа/официального API",
    )


# --- классификация загруженных пользователем актов --------------------------

#: Маркеры судебных органов в тексте/имени файла.
_COURT_MARKERS = re.compile(
    r"(?:верховн\w+ суд\w+|кассационн\w+ суд\w+|апелляционн\w+ суд\w+|"
    r"арбитражн\w+ суд\w+|районн\w+ суд\w+|миров\w+ судья|судебн\w+ коллеги\w+|"
    r"судебн\w+ участок\w+)",
    re.IGNORECASE,
)

#: Маркеры типов судебных актов.
_ACT_TYPE_MARKERS = re.compile(
    r"(?:постановлен\w+|определен\w+|решен\w+|приговор|обзор\w+ практике|"
    r"кассационн\w+ определен\w+|апелляционн\w+ определен\w+)",
    re.IGNORECASE,
)

_CASE_NUMBER_RE = re.compile(r"№\s*([А-ЯA-Z]?\d[\w/-]{3,30})")
_INSTANCE_RE = re.compile(
    r"(перв[а-яё]* инстанци[а-яё]*|апелляцион[а-яё]* инстанци[а-яё]*|"
    r"кассацион[а-яё]* инстанци[а-яё]*|вторая инстанция|надзорн[а-яё]* инстанци[а-яё]*)",
    re.IGNORECASE,
)
_NORM_CITE_RE = re.compile(
    r"(?:ст|стать\w+)\.?\s*(\d+(?:\.\d+)*)\s+([А-ЯA-Z][\w-]*(?:\s+[А-ЯA-Z][\w-]*){0,3})"
)
_TEXT_DATE_RE = re.compile(
    r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})",
    re.IGNORECASE,
)
_NUMERIC_DATE_RE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{4})")
_MONTHS = {
    "января": "01", "февраля": "02", "марта": "03", "апреля": "04", "мая": "05",
    "июня": "06", "июля": "07", "августа": "08", "сентября": "09", "октября": "10",
    "ноября": "11", "декабря": "12",
}


@dataclass
class UserActMetadata:
    """Извлечённые реквизиты судебного акта, загруженного пользователем."""

    court: str | None = None
    decision_date: str | None = None
    case_number: str | None = None
    instance: str | None = None
    cited_norms: list[str] = field(default_factory=list)
    excerpt: str = ""


def classify_user_document(source_name: str, content: str) -> Literal["case_law", "user_document"]:
    """Является ли загруженный документ судебным актом (vs обычным документом).

    Эвристика: маркеры суда И типа акта в тексте или имени файла.
    """
    probe = f"{source_name} {content[:3000]}"
    has_court = bool(_COURT_MARKERS.search(probe))
    has_act = bool(_ACT_TYPE_MARKERS.search(probe))
    return "case_law" if (has_court and has_act) else "user_document"


def extract_user_act_metadata(content: str) -> UserActMetadata:
    """Извлечь суд/дату/номер дела/инстанцию/упомянутые нормы из текста акта.

    Только то, что реально найдено в тексте; ничего не дописывается.
    """
    head = content[:4000]
    meta = UserActMetadata()

    court_match = _COURT_MARKERS.search(head)
    if court_match:
        meta.court = court_match.group(0)

    text_date = _TEXT_DATE_RE.search(head)
    numeric_date = _NUMERIC_DATE_RE.search(head)
    if text_date:
        meta.decision_date = (
            f"{text_date.group(3)}-{_MONTHS[text_date.group(2).lower()]}-{int(text_date.group(1)):02d}"
        )
    elif numeric_date:
        meta.decision_date = (
            f"{numeric_date.group(3)}-{int(numeric_date.group(2)):02d}-{int(numeric_date.group(1)):02d}"
        )

    case_number = _CASE_NUMBER_RE.search(head)
    if case_number:
        meta.case_number = case_number.group(1)

    instance = _INSTANCE_RE.search(head)
    if instance:
        meta.instance = instance.group(1) if instance.lastindex else instance.group(0)

    for norm in _NORM_CITE_RE.finditer(head):
        citation = f"ст. {norm.group(1)} {norm.group(2).strip()}"
        if citation not in meta.cited_norms:
            meta.cited_norms.append(citation)
        if len(meta.cited_norms) >= 5:
            break

    meta.excerpt = re.sub(r"\s+", " ", content[:600]).strip()
    return meta


def user_act_to_legal_source(
    source_name: str,
    content: str,
    *,
    seq: int,
) -> LegalSource:
    """LegalSource из загруженного пользователем судебного акта.

    Уровень ``USER``: источник указал пользователь; реквизиты НЕ считаются
    внешне подтверждёнными до сверки с официальным источником.
    """
    kind = classify_user_document(source_name, content)
    meta = extract_user_act_metadata(content) if kind == "case_law" else UserActMetadata()
    title = re.sub(r"\s+", " ", source_name.replace("case_files/", "")).strip()
    return LegalSource(
        id=f"DOC-{seq:03d}",
        source_type="case_law" if kind == "case_law" else "user_document",
        title=title,
        authority=meta.court or "загружено пользователем",
        citation=meta.case_number or title,
        excerpt=meta.excerpt or content[:400].strip(),
        official_url=None,
        decision_date=meta.decision_date,
        case_number=meta.case_number,
        court=meta.court,
        verified=False,
        verification_status="unverified",
        provider="user_document",
        retrieved_at=now_iso(),
        relevance_score=0.0,
        warning="загружен пользователем; реквизиты не сверены с официальным источником",
        authority_level="USER",
    )


def user_acts_from_materials(fragments) -> list[LegalSource]:
    """Sources практики из загруженных пользователем документов-актов дела.

    :param fragments: последовательность DocumentFragment (source, content).
    """
    sources: list[LegalSource] = []
    for fragment in fragments:
        kind = classify_user_document(fragment.source, fragment.content)
        if kind == "case_law":
            sources.append(
                user_act_to_legal_source(fragment.source, fragment.content, seq=len(sources) + 1)
            )
    return sources

