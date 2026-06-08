"""CPHI scraper (Informa Markets pharma exhibitions).

URL: https://exhibitors.cphi.com/{event_slug}/   (e.g. cpww26)

Pipeline:
  1. Open listing in Playwright, click "Show more" until card count plateaus
  2. For each card: extract name + country + booth + slug (from logo URL or
     name heuristic) + event-scoped exhibitor URL
  3. For each, fetch /company/{slug}/ on cphi-online.com and parse the
     JSON-LD Organization block for email/phone/address
  4. If slug 404s, fall back to scraping the exhibitor URL for the canonical
     "View company profile" link, retry once
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from scraper.core.cache import cache_path, load_cached, store_cached
from scraper.core.jsonld import extract_organization, format_address
from scraper.core.politeness import USER_AGENT, jittered_sleep
from scraper.core.types import Lead, ProgressFn, Scraper, _NULL_PROGRESS

SLUG_FROM_IMG_PATTERN = re.compile(r"/company/([^/?#]+)/")
SHOW_MORE_TEXT_PATTERN = re.compile(r"show\s+more", re.IGNORECASE)
EXHIBITOR_PROFILE_HREF_PATTERN = re.compile(r'href="/company/([^/?#"]+)/"')
PROFILE_URL_TEMPLATE = "https://www.cphi-online.com/company/{slug}/"


@dataclass(frozen=True)
class _Exhibitor:
    """Listing-page extract; not yet enriched with profile data."""

    name: str
    country: str
    booth: str
    detail_url: str
    profile_slug: str


def slugify_company_name(name: str) -> str:
    """Heuristic slug used when a card has no custom logo (so no slug in
    img src). CPHI lowercases, strips accents, and hyphenates."""
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    folded = re.sub(r"[^a-z0-9]+", "-", folded.lower())
    return folded.strip("-")


def _safe_text(locator: Locator) -> str:
    if locator.count() == 0:
        return ""
    return (locator.first.inner_text() or "").strip()


def _safe_attr(locator: Locator, attr: str) -> str:
    if locator.count() == 0:
        return ""
    return (locator.first.get_attribute(attr) or "").strip()


def _extract_slug_from_card(card: Locator, fallback_name: str) -> str:
    for img in card.locator("img[src*='/company/']").all():
        src = img.get_attribute("src") or ""
        m = SLUG_FROM_IMG_PATTERN.search(src)
        if m:
            return m.group(1)
    return slugify_company_name(fallback_name)


def _extract_card(card: Locator) -> _Exhibitor | None:
    name = _safe_text(card.locator(".exhibitor__title h3"))
    if not name:
        return None
    return _Exhibitor(
        name=name,
        country=_safe_text(card.locator(".exhibitor__country")),
        booth=_safe_text(card.locator(".exhibitor__h-place .m-tag__txt")),
        detail_url=_safe_attr(
            card.locator("a.btn-outline-secondary[href*='exhibitor']"), "href"
        ),
        profile_slug=_extract_slug_from_card(card, fallback_name=name),
    )


def _extract_visible_exhibitors(page: Page) -> list[_Exhibitor]:
    seen: set[str] = set()
    out: list[_Exhibitor] = []
    for card in page.locator("div.exhibitor").all():
        ex = _extract_card(card)
        if ex is None:
            continue
        key = ex.detail_url or ex.name
        if key in seen:
            continue
        seen.add(key)
        out.append(ex)
    return out


def _click_show_more(page: Page) -> bool:
    candidate = page.locator("button, a, [role='button']").filter(
        has_text=SHOW_MORE_TEXT_PATTERN
    )
    if candidate.count() == 0:
        return False
    try:
        candidate.first.scroll_into_view_if_needed(timeout=3_000)
        candidate.first.click(timeout=5_000)
        return True
    except PlaywrightTimeoutError:
        return False


def _scrape_listing(
    page: Page, url: str, max_iterations: int, progress: ProgressFn
) -> list[_Exhibitor]:
    progress(f"[listing] opening {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("div.exhibitor", timeout=30_000)
    except PlaywrightTimeoutError:
        progress("[listing] no exhibitor cards appeared within 30s")
        return []

    for i in range(max_iterations):
        current_count = page.locator("div.exhibitor").count()
        progress(f"[listing] iter {i:>3}: {current_count} cards in DOM")
        if not _click_show_more(page):
            progress("[listing] no 'Show more' control found - assumed exhausted")
            break
        jittered_sleep(base=1.5, jitter=0.7)

        grew = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if page.locator("div.exhibitor").count() > current_count:
                grew = True
                break
            time.sleep(0.3)
        if not grew:
            progress(f"[listing] count plateaued at {current_count} - exhausted")
            break

    return _extract_visible_exhibitors(page)


def _fetch_profile_html(
    context: BrowserContext, slug: str, *, referer: str | None
) -> str | None:
    if not slug:
        return None
    page = context.new_page()
    try:
        response = page.goto(
            PROFILE_URL_TEMPLATE.format(slug=slug),
            wait_until="domcontentloaded",
            timeout=45_000,
            referer=referer,
        )
        if response is None or response.status >= 400:
            return None
        page.wait_for_timeout(500)
        return page.content()
    except PlaywrightTimeoutError:
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


def _find_slug_via_exhibitor_page(
    context: BrowserContext, exhibitor_url: str, *, referer: str | None
) -> str | None:
    if not exhibitor_url:
        return None
    page = context.new_page()
    try:
        response = page.goto(
            exhibitor_url,
            wait_until="domcontentloaded",
            timeout=45_000,
            referer=referer,
        )
        if response is None or response.status >= 400:
            return None
        html = page.content()
        m = EXHIBITOR_PROFILE_HREF_PATTERN.search(html)
        return m.group(1) if m else None
    except PlaywrightTimeoutError:
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


def _parse_profile(html: str, slug: str, fallback_country: str) -> Lead:
    org = extract_organization(html)
    profile_url = PROFILE_URL_TEMPLATE.format(slug=slug)
    if not org:
        return Lead(
            company_name="",
            country=fallback_country,
            company_profile_url=profile_url,
        )
    formatted_addr, country_code = format_address(org.get("address"))
    email = (org.get("email") or "").strip()
    website = ""
    if email and "@" in email:
        domain = email.rsplit("@", 1)[-1].strip().lower()
        if domain:
            website = f"https://{domain}"
    return Lead(
        company_name=(org.get("name") or "").strip(),
        country=fallback_country or country_code,
        company_email=email,
        company_phone=(org.get("telephone") or "").strip(),
        company_website=website,
        address=formatted_addr,
        company_profile_url=profile_url,
        email_source="jsonld" if email else "not_found",
        email_confidence="high" if email else "",
    )


class CphiScraper(Scraper):
    site_id = "cphi"
    site_label = "CPHI"

    @classmethod
    def matches(cls, url: str) -> bool:
        return "exhibitors.cphi.com" in url

    def scrape(
        self,
        url: str,
        *,
        cache_dir: Path | None = None,
        max_listing_iterations: int | None = None,
        max_profiles: int | None = None,
        progress: ProgressFn = _NULL_PROGRESS,
    ) -> Iterator[Lead]:
        listing_cap = max_listing_iterations if max_listing_iterations is not None else 200

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            try:
                listing_page = context.new_page()
                exhibitors = _scrape_listing(listing_page, url, listing_cap, progress)
                listing_page.close()

                progress(f"[listing] returned {len(exhibitors)} exhibitors")
                if max_profiles is not None and len(exhibitors) > max_profiles:
                    exhibitors = exhibitors[:max_profiles]
                    progress(f"[listing] capped to {len(exhibitors)} profiles for this run")

                total = len(exhibitors)
                for i, ex in enumerate(exhibitors, 1):
                    if not ex.profile_slug:
                        progress(
                            f"[profile] {i:>4}/{total}: {ex.name!r} - SKIPPED (no slug)"
                        )
                        yield Lead(
                            company_name=ex.name,
                            country=ex.country,
                            booth_number=ex.booth,
                            notes="no profile slug found on listing",
                        )
                        continue

                    slug = ex.profile_slug
                    cf = cache_path(cache_dir, self.site_id, slug)
                    html = load_cached(cf)
                    from_cache = html is not None
                    if html is None:
                        html = _fetch_profile_html(context, slug, referer=url)
                        if html is not None:
                            store_cached(cf, html)

                    slug_corrected = False
                    if html is None and ex.detail_url:
                        real_slug = _find_slug_via_exhibitor_page(
                            context, ex.detail_url, referer=url
                        )
                        if real_slug and real_slug != slug:
                            slug = real_slug
                            slug_corrected = True
                            cf = cache_path(cache_dir, self.site_id, slug)
                            html = load_cached(cf)
                            from_cache = html is not None
                            if html is None:
                                html = _fetch_profile_html(context, slug, referer=url)
                                if html is not None:
                                    store_cached(cf, html)

                    if html is None:
                        progress(f"[profile] {i:>4}/{total}: {ex.name!r} - FETCH FAILED")
                        yield Lead(
                            company_name=ex.name,
                            country=ex.country,
                            booth_number=ex.booth,
                            company_profile_url=PROFILE_URL_TEMPLATE.format(slug=slug),
                            notes="profile fetch failed",
                        )
                    else:
                        lead = _parse_profile(html, slug, fallback_country=ex.country)
                        # Listing data is authoritative for booth; profile may not have it
                        lead = Lead(
                            company_name=lead.company_name or ex.name,
                            country=lead.country or ex.country,
                            booth_number=ex.booth,
                            company_email=lead.company_email,
                            company_phone=lead.company_phone,
                            company_website=lead.company_website,
                            address=lead.address,
                            company_profile_url=lead.company_profile_url,
                            email_source=lead.email_source,
                            email_confidence=lead.email_confidence,
                            notes="slug from exhibitor page" if slug_corrected else "",
                        )
                        tag = "cache" if from_cache else "fresh"
                        if slug_corrected:
                            tag += "+slug-fixed"
                        progress(
                            f"[profile] {i:>4}/{total}: {ex.name!r} ({tag}) "
                            f"email={lead.company_email or '-'} "
                            f"phone={lead.company_phone or '-'}"
                        )
                        yield lead

                    if not from_cache:
                        jittered_sleep()
            finally:
                browser.close()
