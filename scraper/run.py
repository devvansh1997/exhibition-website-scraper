"""CLI entrypoint.

v0.1: scrape a CPHI-style listing page and print the exhibitors found.
No CSV, no detail pages, no email finder yet.

Example:
    python -m scraper.run --url https://exhibitors.cphi.com/cpww26/
"""

from __future__ import annotations

import argparse
import sys

from scraper.listing import scrape_listing


def main() -> int:
    parser = argparse.ArgumentParser(description="Exhibition listing scraper (v0.1)")
    parser.add_argument("--url", required=True, help="Exhibitor listing URL")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on 'Show more' iterations (for testing). Default: unlimited.",
    )
    args = parser.parse_args()

    kwargs = {"max_iterations": args.limit} if args.limit is not None else {}
    exhibitors = scrape_listing(args.url, **kwargs)

    print(f"\n=== Scraped {len(exhibitors)} exhibitors ===\n")
    for ex in exhibitors:
        print(f"  {ex.name:<50}  {ex.country:<25}  booth={ex.booth:<10}  {ex.detail_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
