"""Контракт провайдера правовой информации (шаг 2 этапа 3).

Единый асинхронный интерфейс: ни граф, ни сервис не привязаны к конкретному MCP.
Провайдер обязан быть честным: невозможность выполнить поиск — это статус/пустой
список, а не выдуманные данные.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import LegalSource, ProviderHealth


@runtime_checkable
class LegalProvider(Protocol):
    """Протокол провайдера норм права / судебной практики."""

    name: str

    async def healthcheck(self) -> ProviderHealth:
        """Проверить доступность и вернуть честный статус (не бросает исключений)."""
        ...

    async def search_statutes(
        self,
        query: str,
        jurisdiction: str,
        limit: int = 8,
    ) -> list[LegalSource]:
        """Найти нормативные акты; [] если ничего не найдено или недоступно."""
        ...

    async def get_document(self, source_id: str) -> LegalSource | None:
        """Получить источник по его провайдерному идентификатору (eid/URL/hash)."""
        ...

    async def search_case_law(
        self,
        query: str,
        jurisdiction: str,
        limit: int = 8,
    ) -> list[LegalSource]:
        """Найти судебную практику; провайдер НПА честно возвращает []."""
        ...
