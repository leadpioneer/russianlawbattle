"""Провайдеры правовой информации (этап 3)."""

from __future__ import annotations

from .base import LegalProvider
from .mock import MockLegalProvider

__all__ = ["LegalProvider", "MockLegalProvider"]
