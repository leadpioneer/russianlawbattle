"""Получение машиночитаемого текста статей НПА (markitdown-слой).

Официальный портал pravo.gov.ru отдаёт документы только **сканами** (PDF без
текстового слоя) — из них текст не извлечь. Проверенный живой путь (06.09.2026):

1. на КонсультантПлюс найти базовый документ (страницы статей доступны без
   капчи; текст настоящий, с пометками редакций);
2. в оглавлении базового документа найти ссылку на нужную статью
   (формат URL: ``/document/cons_doc_LAW_<id>/<hash>/``);
3. забрать текст статьи через **markitdown** (Microsoft, MIT) — HTML→Markdown.

Честность источника: текст приходит с КонсультантПлюс (неофициальный сайт),
поэтому в LegalSource он заполняет ``excerpt``, но статус остаётся
``partially_verified`` с warning «текст неофициальный, сверьте с pravo.gov.ru».
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: UA — без него Консультант отдаёт упрощённую страницу.
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 legal-research"

_HTTP_TIMEOUT_S = 60.0

#: «Статья 309» / «Статья 309.1» в тексте ссылки оглавления.
_ARTICLE_LINK_RE = re.compile(
    r'href="(/document/cons_doc_LAW_\d+/[0-9a-f]+/)">Статья\s+(\d+(?:\.\d+)*)\.?\s*([^<]{0,120})'
)

#: Извлечь номер статьи из запроса вида «статья 309 ГК РФ», «ст. 6.1.1 КоАП РФ».
_ARTICLE_NUM_RE = re.compile(r"ст(?:ать\w*|\.?)\s+(\d+(?:\.\d+)*)", re.IGNORECASE)


@dataclass(frozen=True)
class ArticleText:
    """Текст статьи, полученный из внешнего источника."""

    text: str  # очищенный Markdown статьи (без навигации сайта)
    source_url: str  # страница-первоисточник текста
    provider: str  # "consultant" | ...
    title: str | None = None  # заголовок статьи, если распознан


def _clean_consultant_markdown(raw: str) -> str:
    """Убрать навигацию/шапку сайта, оставить содержательную часть статьи."""
    lines = raw.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("# ") and "Статья" in line:
            start = i
            break
    body = lines[start:]
    end_markers = ("Контактная информация", "Все новости", "Производственный календарь")
    cleaned: list[str] = []
    for line in body:
        if any(marker in line for marker in end_markers):
            break
        cleaned.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()


def _markitdown_convert(url: str) -> tuple[str, str | None]:
    """markitdown-конвертация URL → (markdown, title). RuntimeError при сбое."""
    try:
        from markitdown import MarkItDown

        md = MarkItDown(enable_plugins=False)
        result = md.convert(url)
    except Exception as exc:  # noqa: BLE001 — сетевые/парсинг проблемы единообразно
        raise RuntimeError(f"markitdown не смог конвертировать {url}: {exc}") from exc
    text = result.text_content or ""
    if not text.strip():
        raise RuntimeError(f"пустой текст со страницы {url}")
    return text, result.title


def find_consultant_article(article_number: str, base_law_hint: str) -> ArticleText:
    """Найти статью по номеру + подсказке акта и вернуть её текст.

    :param article_number: номер статьи («309», «6.1.1»).
    :param base_law_hint: название/номер базового акта («ГК РФ», «2300-1»,
        «О защите прав потребителей») — для поиска оглавления.
    :raises RuntimeError: если документ/статья не найдены или сеть недоступна.
    """
    import httpx

    # 1. Найти базовый документ: страница «ГК РФ» на Консультанте известна как
    #    /document/cons_doc_LAW_5142/ — ищем через поисковую выдачу сайта.
    with httpx.Client(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        search_resp = client.get(
            "https://www.consultant.ru/search/",
            params={"q": base_law_hint},
            headers={"User-Agent": _UA},
        )
        search_resp.raise_for_status()
        doc_links = re.findall(
            r'href="(/document/cons_doc_LAW_\d+/)"', search_resp.text
        )
        if not doc_links:
            raise RuntimeError(f"базовый документ не найден по запросу {base_law_hint!r}")
        base_path = doc_links[0]

        # 2. Оглавление базового документа → ссылка на статью.
        toc_resp = client.get(
            f"https://www.consultant.ru{base_path}", headers={"User-Agent": _UA}
        )
        toc_resp.raise_for_status()
        for match in _ARTICLE_LINK_RE.finditer(toc_resp.text):
            path, num, heading = match.groups()
            if num == article_number:
                article_url = f"https://www.consultant.ru{path}"
                break
        else:
            raise RuntimeError(
                f"статья {article_number} не найдена в оглавлении {base_path}"
            )

    # 3. Текст статьи через markitdown.
    raw, title = _markitdown_convert(article_url)
    cleaned = _clean_consultant_markdown(raw)
    if not cleaned:
        raise RuntimeError(f"после очистки пусто: {article_url}")
    return ArticleText(text=cleaned, source_url=article_url, provider="consultant", title=title)
