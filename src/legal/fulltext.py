"""Полная сверка источников по полному тексту (поднятие до verified).

Sonar-источники приходят с реквизитами от ИИ и пересказом вместо цитаты —
честный максимум для них ``partially_verified``. Этот модуль скачивает страницу
по ``official_url``, извлекает машиночитаемый текст (markitdown: HTML и PDF)
и ищет в нём реквизиты источника (номер акта, дату, номер статьи).

Критерий: **≥2 различных реквизита** найдены в тексте → выдержка заменяется
реальной цитатой (окно вокруг совпадения). Политика доменов — принцип честности:

- официальные домены (pravo.gov.ru, vsrf.ru) → ``verified=True``;
- неофициальные (sudact.ru и др.) → excerpt заполняется, статус остаётся
  ``partially_verified`` (текст не с официального портала).

Любая ошибка (сеть, пустой текст/скан, реквизиты не найдены) → источник
**без изменений** + warning. Инварианты ``models.py`` не нарушаются: verified
не присваивается без непустого excerpt и реального совпадения.
"""

from __future__ import annotations

import logging
import re

import httpx

from .models import LegalSource

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 30.0
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 legal-research"

#: Домены, публикация на которых подтверждает реквизиты источника.
_OFFICIAL_HOSTS = (
    "pravo.gov.ru", "publication.pravo.gov.ru", "vsrf.ru", "www.vsrf.ru",
    "sudrf.ru", "supcourt.ru",
)

#: Размер выдержки (символов) вокруг первого совпадения реквизитов.
_EXCERPT_WINDOW = 400

#: Номер акта: «№ 2300-1», «№ 17-ФЗ», «266-ФЗ».
_ACT_NUMBER_RE = re.compile(r"\d{1,4}-(?:ФЗ|ФКЗ)\b|№\s*\d{1,4}(?:-\d+)?", re.IGNORECASE)

#: Дата: «07.02.1992».
_DATE_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")

#: Номер статьи: «статья 18», «ст. 6.1.1».
_ARTICLE_NUM_RE = re.compile(r"\bст(?:ать\w*|\.?)\s+(\d+(?:\.\d+)*)", re.IGNORECASE)


def _normalize(text: str) -> str:
    """lower + ё→е + схлопывание пробелов (для устойчивого поиска подстроки)."""
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def _extract_requirement_keys(source: LegalSource) -> set[str]:
    """Нормализованные реквизиты источника: номера актов, даты, номера статей."""
    keys: set[str] = set()
    haystack = f"{source.title} {source.citation} {source.case_number or ''}"
    for match in _ACT_NUMBER_RE.finditer(haystack):
        keys.add(_normalize(match.group(0)))
    for match in _DATE_RE.finditer(haystack):
        keys.add(match.group(0))
    for match in _ARTICLE_NUM_RE.finditer(haystack):
        keys.add(f"ст {match.group(1)}")
    return {k for k in keys if len(k) >= 3}


def _find_key(page_text: str, key: str) -> int:
    """Позиция ключа в тексте: прямое вхождение или текстовый формат даты."""
    pos = page_text.find(key)
    if pos >= 0:
        return pos
    date_match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", key)
    if date_match:
        day, month, year = date_match.groups()
        text_variant = f"{int(day)} {_MONTH_NAMES.get(int(month), '')} {year}"
        if month and text_variant in page_text:
            return page_text.find(text_variant)
    return -1


def _match_count(keys: set[str], page_text: str) -> list[str]:
    """Реквизиты, найденные в тексте страницы (см. :func:`_find_key`)."""
    return [key for key in keys if _find_key(page_text, key) >= 0]


_MONTH_NAMES = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября",
    12: "декабря",
}


def _excerpt_around(page_text: str, key: str) -> str:
    """Окно текста вокруг первого вхождения ключа (реальная цитата)."""
    pos = _find_key(page_text, key)
    if pos < 0:
        return ""
    start = max(0, pos - _EXCERPT_WINDOW // 2)
    end = min(len(page_text), pos + len(key) + _EXCERPT_WINDOW // 2)
    return page_text[start:end].strip()


def _fetch_page_text(url: str) -> str:
    """Скачать URL и превратить в машиночитаемый текст (markitdown: HTML/PDF).

    :raises RuntimeError: сеть/конвертация не удались или текст пуст (скан).
    """
    response = httpx.get(
        url, timeout=_HTTP_TIMEOUT_S, headers={"User-Agent": _UA}, follow_redirects=True
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()

    from markitdown import MarkItDown

    md = MarkItDown(enable_plugins=False)
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name
        try:
            result = md.convert(tmp_path)
        finally:
            os.unlink(tmp_path)
    else:
        result = md.convert(url)
    text = result.text_content or ""
    if len(_normalize(text)) < 200:
        raise RuntimeError(f"текст пуст или слишком короткий ({len(text)} симв.) — скан?")
    return text


def _with_warning(source: LegalSource, warning: str) -> LegalSource:
    """Копия источника с дополнительным warning (существующий сохраняется)."""
    data = source.to_dict()
    data["warning"] = f"{source.warning}; {warning}" if source.warning else warning
    return source.__class__(**data)


def verify_source_fulltext(source: LegalSource) -> LegalSource:
    """Сверить источник с полным текстом страницы; вернуть (возможно) улучшенный.

    Источник без ``official_url`` или уже ``verified`` не обрабатывается.
    Любая ошибка → источник как был (+ warning о причине).
    """
    if not source.official_url or source.verification_status == "verified":
        return source
    keys = _extract_requirement_keys(source)
    if len(keys) < 2:
        return source  # реквизитов не хватает для надёжной сверки
    try:
        page_text = _normalize(_fetch_page_text(source.official_url))
    except Exception as exc:  # noqa: BLE001 — ошибка сверки не роняет сборку pack
        logger.info("fulltext: %s не скачан: %s", source.id, exc)
        return _with_warning(source, f"полный текст не сверён: {type(exc).__name__}")

    matched = _match_count(keys, page_text)
    if len(matched) < 2:
        return _with_warning(source, "реквизиты не найдены в полном тексте страницы")

    excerpt = _excerpt_around(page_text, matched[0])
    host = source.official_url.split("/")[2].lower()
    official = any(host == h or host.endswith("." + h) for h in _OFFICIAL_HOSTS)
    data = source.to_dict()
    data["excerpt"] = excerpt
    if official:
        data.update(verified=True, verification_status="verified", warning=None)
    else:
        data["warning"] = (
            "текст сверён, но источник неофициальный — сверьте реквизиты по pravo.gov.ru"
        )
    return source.__class__(**data)
