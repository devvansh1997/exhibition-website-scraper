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
import sys
from datetime import datetime, timezone
from pathlib import Path

from scraper.core.csv_writer import lead_to_row, output_path, write_csv
from scraper.core.types import RunMetadata
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

    rows: list[dict] = []
    stats = {"with_email": 0, "with_phone": 0, "fetch_failed": 0}

    for lead in scraper.scrape(
        args.url,
        cache_dir=cache_dir,
        max_listing_iterations=args.limit_iterations,
        max_profiles=args.limit_profiles,
        progress=print,
    ):
        if lead.company_email:
            stats["with_email"] += 1
        if lead.company_phone:
            stats["with_phone"] += 1
        if "fetch failed" in (lead.notes or ""):
            stats["fetch_failed"] += 1
        rows.append(lead_to_row(lead, meta))

    out = output_path(meta, output_dir)
    write_csv(rows, out)

    total = len(rows)
    pct = lambda x: (x * 100 // total) if total else 0
    print(
        f"\n[run] wrote {total} rows to {out}\n"
        f"      with_email={stats['with_email']} ({pct(stats['with_email'])}%)  "
        f"with_phone={stats['with_phone']} ({pct(stats['with_phone'])}%)  "
        f"fetch_failed={stats['fetch_failed']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
