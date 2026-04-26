"""CLI entrypoint for the scraper.

v0.2: scrape a CPHI listing -> for each exhibitor, fetch /company/{slug}/
and parse the JSON-LD Organization block -> write a CSV with name,
country, email, phone, address, etc.

Example:
    python -m scraper.run \\
        --url https://exhibitors.cphi.com/cpww26/ \\
        --exhibition-name "CPHI Milan" \\
        --exhibition-year 2026 \\
        --industry Pharma
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from scraper.csv_writer import RunMetadata, output_path, write_csv
from scraper.listing import USER_AGENT, Exhibitor, scrape_listing
from scraper.profile import (
    Profile,
    fetch_profile_html,
    find_slug_via_exhibitor_page,
    parse_profile_html,
    profile_url_for,
)


def _jittered_sleep(base: float = 2.0, jitter: float = 1.0) -> None:
    time.sleep(base + random.uniform(0, jitter))


def _derive_website_from_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    domain = email.rsplit("@", 1)[-1].strip().lower()
    return f"https://{domain}" if domain else ""


def _load_or_fetch_html(
    context,
    slug: str,
    cache_dir: Path | None,
    referer: str | None,
) -> tuple[str | None, bool]:
    """Returns (html, from_cache). cache_dir=None disables caching."""
    cache_file = cache_dir / "profile" / f"{slug}.html" if cache_dir else None
    if cache_file is not None and cache_file.exists():
        return cache_file.read_text(encoding="utf-8"), True
    html = fetch_profile_html(context, slug, referer=referer)
    if html is not None and cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(html, encoding="utf-8")
    return html, False


def _build_row(
    meta: RunMetadata,
    ex: Exhibitor,
    profile: Profile | None,
    notes: str = "",
) -> dict:
    email = profile.email if profile else ""
    if email:
        email_source = "jsonld"
        email_confidence = "high"
    else:
        email_source = "not_found"
        email_confidence = ""
    website = _derive_website_from_email(email)
    return {
        "exhibition_name": meta.exhibition_name,
        "exhibition_year": meta.exhibition_year,
        "industry": meta.industry,
        "exhibition_url": meta.exhibition_url,
        "scraped_at": meta.scraped_at,
        "company_name": (profile.name if profile and profile.name else ex.name),
        "country": ex.country or (profile.address_country if profile else ""),
        "booth_number": ex.booth,
        "company_email": email,
        "company_phone": profile.phone if profile else "",
        "company_website": website,
        "address": profile.address if profile else "",
        "company_profile_url": (
            profile.profile_url if profile else (profile_url_for(ex.profile_slug) if ex.profile_slug else "")
        ),
        "email_source": email_source,
        "email_confidence": email_confidence,
        "notes": notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Exhibition listing + profile scraper (v0.2)")
    parser.add_argument("--url", required=True, help="Exhibitor listing URL")
    parser.add_argument("--exhibition-name", required=True, help='e.g. "CPHI Milan"')
    parser.add_argument("--exhibition-year", type=int, required=True, help="e.g. 2026")
    parser.add_argument("--industry", required=True, help='e.g. "Pharma"')
    parser.add_argument("--limit-iterations", type=int, default=None, help="Cap on listing 'Show more' clicks")
    parser.add_argument("--limit-profiles", type=int, default=None, help="Cap on number of profile fetches")
    parser.add_argument("--no-cache", action="store_true", help="Bypass HTML cache for profiles")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--cache-dir", default="cache")
    args = parser.parse_args()

    scraped_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    meta = RunMetadata(
        exhibition_name=args.exhibition_name,
        exhibition_year=args.exhibition_year,
        industry=args.industry,
        exhibition_url=args.url,
        scraped_at=scraped_at,
    )
    output_dir = Path(args.output_dir)
    cache_dir: Path | None = None if args.no_cache else Path(args.cache_dir)

    print(f"[run] scraping listing: {args.url}")
    listing_kwargs = {"max_iterations": args.limit_iterations} if args.limit_iterations is not None else {}
    exhibitors = scrape_listing(args.url, **listing_kwargs)
    print(f"[run] listing returned {len(exhibitors)} exhibitors")
    if args.limit_profiles is not None:
        exhibitors = exhibitors[: args.limit_profiles]
        print(f"[run] capped to {len(exhibitors)} profiles for this run")

    rows: list[dict] = []
    stats = {"with_email": 0, "with_phone": 0, "no_slug": 0, "fetch_failed": 0, "cache_hits": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)

        for i, ex in enumerate(exhibitors, 1):
            if not ex.profile_slug:
                stats["no_slug"] += 1
                rows.append(_build_row(meta, ex, profile=None, notes="no profile slug found on listing"))
                print(f"[profile] {i:>4}/{len(exhibitors)}: {ex.name!r} - SKIPPED (no slug)")
                continue

            slug = ex.profile_slug
            html, from_cache = _load_or_fetch_html(
                context,
                slug,
                cache_dir=cache_dir,
                referer=args.url,
            )
            if from_cache:
                stats["cache_hits"] += 1

            # Fallback: if our slug guess 404'd, scrape the event-scoped
            # exhibitor page to find the canonical slug, then retry.
            slug_corrected = False
            if html is None and ex.detail_url:
                real_slug = find_slug_via_exhibitor_page(context, ex.detail_url, referer=args.url)
                if real_slug and real_slug != slug:
                    slug = real_slug
                    slug_corrected = True
                    html, from_cache_2 = _load_or_fetch_html(
                        context,
                        slug,
                        cache_dir=cache_dir,
                        referer=args.url,
                    )
                    if from_cache_2:
                        stats["cache_hits"] += 1

            if html is None:
                stats["fetch_failed"] += 1
                rows.append(_build_row(meta, ex, profile=None, notes="profile fetch failed"))
                print(f"[profile] {i:>4}/{len(exhibitors)}: {ex.name!r} - FETCH FAILED")
            else:
                profile = parse_profile_html(html, slug)
                if profile.email:
                    stats["with_email"] += 1
                if profile.phone:
                    stats["with_phone"] += 1
                note = "slug from exhibitor page" if slug_corrected else ""
                rows.append(_build_row(meta, ex, profile, notes=note))
                tag = "cache" if from_cache else "fresh"
                if slug_corrected:
                    tag += "+slug-fixed"
                print(
                    f"[profile] {i:>4}/{len(exhibitors)}: {ex.name!r} ({tag}) "
                    f"email={profile.email or '-'} phone={profile.phone or '-'}"
                )

            if not from_cache:
                _jittered_sleep()

        browser.close()

    out = output_path(meta, output_dir)
    write_csv(rows, out)

    total = len(rows)
    print(
        f"\n[run] wrote {total} rows to {out}\n"
        f"      with_email={stats['with_email']} ({stats['with_email'] * 100 // max(total, 1)}%)  "
        f"with_phone={stats['with_phone']} ({stats['with_phone'] * 100 // max(total, 1)}%)  "
        f"cache_hits={stats['cache_hits']}  fetch_failed={stats['fetch_failed']}  "
        f"no_slug={stats['no_slug']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
