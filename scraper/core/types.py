"""Shared dataclasses and the Scraper base class.

Every per-site scraper produces a stream of `Lead` objects. The orchestrator
combines them with `RunMetadata` (constant per run) to write the final CSV.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


@dataclass(frozen=True)
class Lead:
    """One exhibitor row. Excludes per-run metadata (added by orchestrator)."""

    company_name: str
    country: str = ""
    booth_number: str = ""
    company_email: str = ""
    company_phone: str = ""
    company_website: str = ""
    address: str = ""
    company_profile_url: str = ""
    email_source: str = "not_found"  # jsonld | dom | not_found
    email_confidence: str = ""  # high | medium | low | ""
    notes: str = ""


@dataclass(frozen=True)
class RunMetadata:
    exhibition_name: str
    exhibition_year: int
    industry: str
    exhibition_url: str
    scraped_at: str  # YYYYMMDDTHHMMSSZ, file-system safe


ProgressFn = Callable[[str], None]
_NULL_PROGRESS: ProgressFn = lambda _s: None  # noqa: E731


class Scraper:
    """Base class. Each site subclass implements `matches` and `scrape`."""

    site_id: str = ""
    site_label: str = ""

    @classmethod
    def matches(cls, url: str) -> bool:
        """Return True if this scraper handles URLs like `url`."""
        raise NotImplementedError

    def scrape(
        self,
        url: str,
        *,
        cache_dir: Path | None = None,
        max_listing_iterations: int | None = None,
        max_profiles: int | None = None,
        progress: ProgressFn = _NULL_PROGRESS,
    ) -> Iterator[Lead]:
        """Yield Lead objects as exhibitors are scraped.

        - `cache_dir`: when set, sites may cache HTML on disk under
          `cache_dir/{site_id}/...` to make re-runs cheaper.
        - `max_listing_iterations`: cap on listing pagination (testing).
        - `max_profiles`: cap on number of leads yielded (testing).
        - `progress`: called with status strings like
          "[profile]  37/120: 'Acme' (fresh) email=info@..." for the
          orchestrator to print or log.
        """
        raise NotImplementedError
