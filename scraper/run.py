"""CLI entrypoint. Picks a scraper by URL and writes a CSV.

Example:
    python -m scraper.run \\
        --url https://exhibitors.cphi.com/cpww26/ \\
        --exhibition-name "CPHI Milan" \\
        --exhibition-year 2026 \\
        --industry Pharma
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from scraper.core.csv_writer import lead_to_row, output_path, write_csv
from scraper.core.politeness import USER_AGENT, jittered_sleep
from scraper.core.types import Lead, RunMetadata
from scraper.core.website_email import find_email_for_website
from scraper.registry import pick_scraper


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-site exhibition scraper")
    parser.add_argument("--url", required=True, help="Exhibitor listing URL")
    parser.add_argument("--exhibition-name", required=True, help='e.g. "CPHI Milan"')
    parser.add_argument("--exhibition-year", type=int, required=True, help="e.g. 2026")
    parser.add_argument("--industry", required=True, help='e.g. "Pharma"')
    parser.add_argument(
        "--limit-iterations",
        type=int,
        default=None,
        help="Cap on listing pagination clicks (testing)",
    )
    parser.add_argument(
        "--limit-profiles",
        type=int,
        default=None,
        help="Cap on number of leads produced (testing)",
    )
    parser.add_argument("--no-cache", action="store_true", help="Bypass HTML cache")
    parser.add_argument(
        "--no-gap-fill",
        action="store_true",
        help="Skip the post-scrape pass that hits each company's own website "
             "to recover an email. Faster, but lower coverage on sites that "
             "don't publish exhibitor emails (e.g. Space Tech Expo).",
    )
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--cache-dir", default="cache")
    args = parser.parse_args()

    scraper = pick_scraper(args.url)
    print(f"[run] using scraper: {scraper.site_id} ({scraper.site_label})")

    scraped_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    meta = RunMetadata(
        exhibition_name=args.exhibition_name,
        exhibition_year=args.exhibition_year,
        industry=args.industry,
        exhibition_url=args.url,
        scraped_at=scraped_at,
    )
    output_dir = Path(args.output_dir)
    cache_dir = None if args.no_cache else Path(args.cache_dir)

    # Phase 1: scrape the platform into Leads.
    leads: list[Lead] = []
    for lead in scraper.scrape(
        args.url,
        cache_dir=cache_dir,
        max_listing_iterations=args.limit_iterations,
        max_profiles=args.limit_profiles,
        progress=print,
    ):
        leads.append(lead)

    # Phase 2: for leads with no email but a known website, hit the
    # company's own site and try to find one. Skipped with --no-gap-fill.
    gap_filled = 0
    gap_attempted = 0
    if not args.no_gap_fill:
        candidates = [
            (i, l)
            for i, l in enumerate(leads)
            if not l.company_email and l.company_website
        ]
        if candidates:
            print(f"\n[gapfill] {len(candidates)} leads need email — visiting their websites")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=USER_AGENT)
                try:
                    for idx, (i, lead) in enumerate(candidates, 1):
                        gap_attempted += 1
                        print(
                            f"[gapfill] {idx:>4}/{len(candidates)}: "
                            f"{lead.company_name!r} -> {lead.company_website}"
                        )
                        try:
                            email = find_email_for_website(
                                context,
                                lead.company_website,
                                cache_dir=cache_dir,
                                progress=print,
                            )
                        except Exception as e:
                            print(f"  [gapfill] error: {e!r}")
                            email = ""
                        if email:
                            leads[i] = dataclasses.replace(
                                lead,
                                company_email=email,
                                email_source="company_website",
                                email_confidence="medium",
                            )
                            gap_filled += 1
                        # Politeness across DIFFERENT external domains —
                        # gentler than within-site throttle since each
                        # company sees only one of our requests.
                        jittered_sleep(base=1.0, jitter=0.5)
                finally:
                    browser.close()

    # Phase 3: write CSV + summary.
    rows = [lead_to_row(l, meta) for l in leads]
    out = output_path(meta, output_dir)
    write_csv(rows, out)

    total = len(rows)
    with_email = sum(1 for l in leads if l.company_email)
    with_phone = sum(1 for l in leads if l.company_phone)
    fetch_failed = sum(1 for l in leads if "fetch failed" in (l.notes or ""))
    pct = lambda x: (x * 100 // total) if total else 0
    print(
        f"\n[run] wrote {total} rows to {out}\n"
        f"      with_email={with_email} ({pct(with_email)}%)  "
        f"with_phone={with_phone} ({pct(with_phone)}%)  "
        f"fetch_failed={fetch_failed}  "
        f"gap_filled={gap_filled}/{gap_attempted}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
