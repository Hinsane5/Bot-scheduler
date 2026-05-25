"""Scraper registry."""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from typing import TYPE_CHECKING

from src.scrapers.base import Scraper

if TYPE_CHECKING:
    from src.scrapers.lms import LMSScraper
    from src.scrapers.messier import MessierScraper


class ScraperRegistry(MutableMapping[str, Scraper]):
    def __init__(self) -> None:
        self._scrapers: dict[str, Scraper] = {}

    def __getitem__(self, key: str) -> Scraper:
        self._ensure_loaded()
        return self._scrapers[key]

    def __setitem__(self, key: str, value: Scraper) -> None:
        self._scrapers[key] = value

    def __delitem__(self, key: str) -> None:
        self._ensure_loaded()
        del self._scrapers[key]

    def __iter__(self) -> Iterator[str]:
        self._ensure_loaded()
        return iter(self._scrapers)

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._scrapers)

    def _ensure_loaded(self) -> None:
        if self._scrapers:
            return
        from src.scrapers.lms import LMSScraper
        from src.scrapers.messier import MessierScraper

        self._scrapers["lms"] = LMSScraper()
        self._scrapers["messier"] = MessierScraper()


REGISTRY: MutableMapping[str, Scraper] = ScraperRegistry()
