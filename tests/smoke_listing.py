"""Listing smoke test — catches silent DOM-change breakage.

Exhibition sites rewrite their markup without warning. When they do, a
scraper can quietly start returning zero exhibitors and nobody notices
until the client gets an empty CSV (this is exactly what happened to
EuroTier when it dropped the "/newfront" URL prefix).

This test runs ONLY the listing walk for each site (no detail fetches,
no gap-fill — max_profiles=0), reads the "[listing] returned N
exhibitors" count each scraper reports, and asserts it cleared a low
threshold. It's a canary, not a coverage test: a red run means "a
listing selector broke, go look", not "the data is perfect".

Run locally:
    python -m tests.smoke_listing

Exit code 0 = all sites healthy, 1 = at least one site below threshold
or errored.
"""

from __future__ import annotations

import re
import sys
import traceback

from scraper.registry import SCRAPERS

# Known-good listing URLs — the client's actual target shows. Update the
# event slug/year here when a new edition goes live.
SITE_URLS: dict[str, str] = {
    "cphi": "https://exhibitors.cphi.com/cpww26/",
    "electronica": "https://exhibitors.electronica.de/exhibitor-portal/2026/",
    "eurotier": "https://digital.eurotier.com/newfront/marketplace/exhibitors?pageNumber=1&limit=60",
    "figlobal": "https://exhibitors.figlobal.com/live/figlobal/event46.jsp?site=47&type=company&eventid=629&map=false",
    "spacetechexpo": "https://www.spacetechexpo-europe.com/exhibitor-list/",
}

# Minimum exhibitors we expect from a single listing iteration/page on a
# healthy site. A broken selector drops the count to ~0, so a low bar of 5
# still catches breakage while leaving margin for a slow cold CI runner
# that only gets part-way through the first batch (healthy sites return
# 10-600+ here).
MIN_EXHIBITORS = 5

_RETURNED_RE = re.compile(r"\[listing\]\s+returned\s+(\d+)\s+exhibitors", re.IGNORECASE)


def _listing_count_for(scraper, url: str) -> int:
    """Run just the listing walk and return the exhibitor count the
    scraper reported. Consumes the (empty) generator so the scrape
    actually executes."""
    counts: list[int] = []

    def capture(line: str) -> None:
        m = _RETURNED_RE.search(line)
        if m:
            counts.append(int(m.group(1)))

    # max_profiles=0 -> listing runs, but no detail pages are fetched.
    # max_listing_iterations=1 -> a single page/batch is enough to prove
    # the listing selector still matches.
    for _lead in scraper.scrape(
        url,
        cache_dir=None,
        max_listing_iterations=1,
        max_profiles=0,
        progress=capture,
    ):
        pass  # no leads are yielded at max_profiles=0

    return max(counts) if counts else 0


def main() -> int:
    results: list[tuple[str, str, int]] = []  # (site_id, status, count)
    failures = 0

    for cls in SCRAPERS:
        site_id = cls.site_id
        url = SITE_URLS.get(site_id)
        if not url:
            results.append((site_id, "NO-URL", 0))
            failures += 1
            print(f"[smoke] {site_id}: no known URL configured", file=sys.stderr)
            continue

        try:
            count = _listing_count_for(cls(), url)
        except Exception:
            results.append((site_id, "ERROR", 0))
            failures += 1
            print(f"[smoke] {site_id}: EXCEPTION", file=sys.stderr)
            traceback.print_exc()
            continue

        ok = count >= MIN_EXHIBITORS
        results.append((site_id, "OK" if ok else "LOW", count))
        if not ok:
            failures += 1

    print("\n===== listing smoke test =====")
    for site_id, status, count in results:
        flag = "PASS" if status == "OK" else "FAIL"
        print(f"  [{flag}] {site_id:<16} status={status:<7} exhibitors={count}")
    print(f"threshold: >= {MIN_EXHIBITORS} exhibitors from one listing iteration")

    if failures:
        print(f"\n{failures} site(s) failed — a listing selector has likely broken.")
        return 1
    print("\nall sites healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
