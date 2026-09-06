"""Извлечение правовых вопросов из материалов дела (шаг 4 этапа 3).

Один короткий LLM-вызов формулирует структурированное описание дела: тип спора,
процессуальная ветка, требования, возражения, ключевые юридические вопросы
и факты, требующие доказывания. Строгая валидация JSON: мусор модели →
fallback (вопросы из свободного текста пользователя).

Результат — только поисковые подсказки. Он НЕ является доказательством и не
попадает в отчёт как факт.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Ограничение на длину текста вопроса в запросе (бережём контекст LLM).
_MAX_ISSUE_LEN = 200


@dataclass
class LegalIssues:
    """Структурированные правовые вопросы дела."""

    case_type: str = ""  # «защита прав потребителей», «взыскание долга»…
    procedure: str = ""  # «гражданский процесс», «арбитражный процесс»…
    claims: list[str] = field(default_factory=list)  # требования заявителя
    objections: list[str] = field(default_factory=list)  # возможные возражения
    issues: list[str] = field(default_factory=list)  # ключевые правовые вопросы
    facts_to_prove: list[str] = field(default_factory=list)  # что доказывать
    source: str = "llm"  # llm | fallback | user

    def to_dict(self) -> dict:
        return {
            "case_type": self.case_type,
            "procedure": self.procedure,
            "claims": list(self.claims),
            "objections": list(self.objections),
            "issues": list(self.issues),
            "facts_to_prove": list(self.facts_to_prove),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LegalIssues":
        return cls(
            case_type=str(data.get("case_type", "")),
            procedure=str(data.get("procedure", "")),
            claims=list(data.get("claims", [])),
            objections=list(data.get("objections", [])),
            issues=list(data.get("issues", [])),
            facts_to_prove=list(data.get("facts_to_prove", [])),
            source=str(data.get("source", "llm")),
        )

    def search_queries(self, limit: int = 4) -> list[str]:
        """Поисковые запросы для провайдеров: вопросы + тип спора."""
        queries = [*self.issues[:limit]]
        if self.case_type and self.case_type not in " ".join(queries):
            queries.append(self.case_type)
        return [q[:_MAX_ISSUE_LEN] for q in queries if q.strip()][:limit]


_EXTRACT_SYSTEM_PROMPT = (
    "Ты — юридический аналитик. Проанализируй описание дела и верни СТРОГО JSON "
    "без markdown-обёрток по схеме:\n"
    '{"case_type": "тип спора кратко", "procedure": "гражданский процесс|арбитражный процесс|'
    'административное производство|неизвестно", "claims": ["требования заявителя"], '
    '"objections": ["возможные возражения ответчика"], "issues": ["ключевые правовые вопросы '
    'через нормы права"], "facts_to_prove": ["факты, которые нужно доказать"]}\n'
    "Каждый список — 1-4 коротких пункта. Не выдумывай обстоятельств, которых нет в деле."
)


def _extract_json_object(text: str) -> dict | None:
    """Достать JSON-объект из ответа модели (терпимо к ```json-обёрткам)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _fallback_issues(context: str, materials_summary: str) -> LegalIssues:
    """Fallback без LLM: свободный текст пользователя становится вопросом."""
    text = " ".join(part for part in (context, materials_summary) if part).strip()
    if not text:
        return LegalIssues(source="fallback")
    sentences = re.split(r"(?<=[.!?])\s+", text)[:3]
    issues = [s.strip()[:_MAX_ISSUE_LEN] for s in sentences if len(s.strip()) > 10]
    return LegalIssues(issues=issues or [text[:_MAX_ISSUE_LEN]], source="fallback")


def extract_issues(
    context: str,
    materials_summary: str,
    jurisdiction: str,
    *,
    free_text_query: str | None = None,
    use_llm: bool = True,
) -> LegalIssues:
    """Извлечь правовые вопросы из материалов дела.

    :param context: case_context (суть спора от пользователя).
    :param materials_summary: выжимка загруженных документов.
    :param jurisdiction: юрисдикция из конфига.
    :param free_text_query: свободный текст вопроса (fallback, если LLM недоступна).
    :param use_llm: False — сразу fallback без LLM (тесты/offline).
    """
    if free_text_query and not use_llm:
        return LegalIssues(issues=[free_text_query[:_MAX_ISSUE_LEN]], source="user")
    if not use_llm:
        return _fallback_issues(context, materials_summary)

    try:
        from ..llm_client import chat

        user_prompt = (
            f"Юрисдикция: {jurisdiction}\n\n"
            f"Описание дела от пользователя:\n{context[:4000] or '(нет)'}\n\n"
            f"Выжимка документов:\n{materials_summary[:4000] or '(нет)'}\n\n"
            "Верни JSON по схеме."
        )
        raw = chat(
            role="judge",
            system_prompt=_EXTRACT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            temperature=0.2,
        )
        data = _extract_json_object(str(raw))
        if not data:
            raise ValueError("модель вернула не JSON")
        issues = LegalIssues.from_dict(data)
        if not issues.issues:
            raise ValueError("пустой список issues")
        issues.source = "llm"
        return issues
    except Exception as exc:  # noqa: BLE001 — любая проблема LLM → fallback
        logger.warning("extract_issues: LLM недоступна (%s) — fallback", exc)
        if free_text_query:
            return LegalIssues(issues=[free_text_query[:_MAX_ISSUE_LEN]], source="user")
        return _fallback_issues(context, materials_summary)

