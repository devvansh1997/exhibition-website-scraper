"""URL -> Scraper picker.

To add a new site, import its scraper class and add it to `SCRAPERS`.
The first one whose `matches(url)` returns True wins.
"""

from __future__ import annotations

from scraper.core.types import Scraper
from scraper.sites.cphi import CphiScraper

SCRAPERS: list[type[Scraper]] = [
    CphiScraper,
]


def pick_scraper(url: str) -> Scraper:
    for cls in SCRAPERS:
        if cls.matches(url):
            return cls()
    supported = ", ".join(cls.site_id for cls in SCRAPERS)
    raise ValueError(
        f"No scraper registered for URL {url!r}. Supported sites: {supported}"
    )
