"""Base scraper interface.

Implemented in Phase 3.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from src.parser import Event


class Scraper(ABC):
    name: str

    @abstractmethod
    async def fetch(self, start: datetime, end: datetime) -> list[Event]:
        """Return events within the inclusive start/end window."""

