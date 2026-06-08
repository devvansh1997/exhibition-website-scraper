"""Fi Global / FI Europe (figlobal.com) scraper.

URL: https://exhibitors.figlobal.com/live/figlobal/event46.jsp?...

Same Informa platform as CPHI but a different vintage of the
exhibitor profile system: there is NO /company/{slug}/ JSON-LD page
to lean on. Detail data has to come from the event-scoped
ingredientsnetwork.com/47/.../exhibitor{ID}-629.html page itself.

Pipeline:
  1. Open listing (JS-rendered), click "Show more results" until
     card count plateaus (same UX as CPHI).
  2. For each div.exhibitor card extract: name (h4 minus 'Featured'
     badge text), booth (.stand), country (.country), data-companyid.
  3. Derive the detail URL from companyid:
     ingredientsnetwork.com/47/company/{AA}/{BB}/{CC}/exhibitor{ID}-629.html
     (companyid split into 2-char groups, zero-padded to 6 digits).
  4. Fetch the detail page; email is in plain body text (not mailto:),
     website is the first non-Informa external link.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from scraper.core.cache import cache_path, load_cached, store_cached
from scraper.core.politeness import USER_AGENT, jittered_sleep
from scraper.core.types import Lead, ProgressFn, Scraper, _NULL_PROGRESS

SHOW_MORE_TEXT_PATTERN = re.compile(r"show\s+more", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
STAND_BODY_RE = re.compile(r"STAND\s+([A-Z0-9.\-]+)", re.IGNORECASE)

# Show / parent / sister / social / search hosts we never want as the
# company's "website" or "email".
SKIP_HOSTS = {
    "figlobal.com",
    "ingredientsnetwork.com",
    "cphi-online.com",
    "cphi.com",
    "informa.com",
    "informamarkets.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "facebook.com",
    "instagram.com",
    "google.com",
}
SKIP_EMAIL_DOMAINS = SKIP_HOSTS | {
    "example.com",
}


@dataclass(frozen=True)
class _Listing:
    company_id: str
    name: str
    country: str
    booth: str
    categories: str
    detail_url: str


def _safe_text(loc: Locator) -> str:
    if loc.count() == 0:
        return ""
    return (loc.first.inner_text() or "").strip()


def _derive_detail_url(company_id: str) -> str:
    cid = str(company_id).strip()
    if not cid.isdigit():
        return ""
    padded = cid.zfill(6)
    return (
        "https://www.ingredientsnetwork.com/47/company/"
        f"{padded[:2]}/{padded[2:4]}/{padded[4:6]}/exhibitor{cid}-629.html"
    )


def _extract_card(card: Locator) -> _Listing | None:
    # Company ID lives on the .toggler child div as data-companyid.
    toggler = card.locator("[data-companyid]").first
    if not toggler or card.locator("[data-companyid]").count() == 0:
        return None
    company_id = (toggler.get_attribute("data-companyid") or "").strip()
    if not company_id:
        return None
    # Prefer the canonical name in data-companyname (the h4 inner_text
    # picks up the "FEATURED"/"NEW" badge text alongside the name).
    name = (toggler.get_attribute("data-companyname") or "").strip()
    if not name:
        # Fallback: scrape h4, strip badge prefix (case-insensitive, may
        # be uppercased by CSS text-transform).
        raw = _safe_text(card.locator("h4"))
        parts = [p.strip() for p in raw.splitlines() if p.strip()]
        parts = [p for p in parts if p.lower() not in ("featured", "new")]
        name = " ".join(parts) if parts else raw
    if not name:
        return None
    booth = _safe_text(card.locator(".stand"))
    country = _safe_text(card.locator(".country"))
    cats = " | ".join(
        (a.inner_text() or "").strip()
        for a in card.locator(".categories a.subcategory").all()
    )
    cats = re.sub(r"\s*\|\s*", "; ", cats)
    return _Listing(
        company_id=company_id,
        name=name,
        country=country,
        booth=booth,
        categories=cats,
        detail_url=_derive_detail_url(company_id),
    )


def _extract_visible(page: Page) -> list[_Listing]:
    out: list[_Listing] = []
    seen: set[str] = set()
    for card in page.locator("div.exhibitor").all():
        ex = _extract_card(card)
        if ex is None or ex.company_id in seen:
            continue
        seen.add(ex.company_id)
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


def _walk_listing(
    page: Page, listing_url: str, max_iterations: int, progress: ProgressFn
) -> list[_Listing]:
    progress(f"[listing] opening {listing_url}")
    page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector("div.exhibitor", timeout=30_000)
    except PlaywrightTimeoutError:
        progress("[listing] no exhibitor cards appeared")
        return []

    for i in range(max_iterations):
        current = page.locator("div.exhibitor").count()
        progress(f"[listing] iter {i:>3}: {current} cards in DOM")
        if not _click_show_more(page):
            progress("[listing] no 'Show more' control - assumed exhausted")
            break
        jittered_sleep(base=1.5, jitter=0.7)
        grew = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if page.locator("div.exhibitor").count() > current:
                grew = True
                break
            time.sleep(0.3)
        if not grew:
            progress(f"[listing] count plateaued at {current} - exhausted")
            break

    return _extract_visible(page)


def _is_skippable_host(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


def _pick_website(page: Page) -> str:
    for a in page.locator("a[href^='http']").all():
        href = a.get_attribute("href") or ""
        try:
            host = urlparse(href).netloc
        except ValueError:
            continue
        if host and not _is_skippable_host(host):
            return href
    return ""


def _pick_email_from_html(html: str, body_text: str) -> str:
    """Figlobal embeds the company email as plain text (no mailto:).
    Pull the first non-infra address out of the body text."""
    for source in (body_text, html):
        for em in EMAIL_RE.findall(source):
            domain = em.rsplit("@", 1)[-1].lower()
            if any(
                domain == d or domain.endswith("." + d) for d in SKIP_EMAIL_DOMAINS
            ):
                continue
            return em
    return ""


def _parse_detail(page: Page, listing: _Listing, html: str) -> Lead:
    try:
        body = page.locator("body").inner_text()
    except Exception:
        body = ""
    email = _pick_email_from_html(html, body)
    website = _pick_website(page)
    # Booth: prefer listing's .stand; fall back to "STAND X" body pattern
    booth = listing.booth
    if not booth:
        m = STAND_BODY_RE.search(body)
        if m:
            booth = m.group(1)
    return Lead(
        company_name=listing.name,
        country=listing.country,
        booth_number=booth,
        company_email=email,
        company_phone="",  # not exposed on Fi profile pages
        company_website=website,
        address="",  # not exposed on Fi profile pages
        company_profile_url=listing.detail_url,
        email_source="dom" if email else "not_found",
        email_confidence="medium" if email else "",
        notes=("categories: " + listing.categories) if listing.categories else "",
    )


def _fetch_detail_html(
    context: BrowserContext, url: str, *, referer: str | None
) -> str | None:
    page = context.new_page()
    try:
        r = page.goto(url, wait_until="domcontentloaded", timeout=45_000, referer=referer)
        if r is None or r.status >= 400:
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


class FiGlobalScraper(Scraper):
    site_id = "figlobal"
    site_label = "Fi Global"

    @classmethod
    def matches(cls, url: str) -> bool:
        return "exhibitors.figlobal.com" in url

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
                lp = context.new_page()
                listings = _walk_listing(lp, url, listing_cap, progress)
                lp.close()
                progress(f"[listing] returned {len(listings)} exhibitors")
                if max_profiles is not None and len(listings) > max_profiles:
                    listings = listings[:max_profiles]
                    progress(f"[listing] capped to {len(listings)} profiles")

                total = len(listings)
                for i, lst in enumerate(listings, 1):
                    if not lst.detail_url:
                        progress(
                            f"[profile] {i:>4}/{total}: {lst.name!r} - SKIPPED (no companyid)"
                        )
                        yield Lead(
                            company_name=lst.name,
                            country=lst.country,
                            booth_number=lst.booth,
                            notes="no companyid found on listing",
                        )
                        continue

                    cf = cache_path(cache_dir, self.site_id, lst.company_id)
                    html = load_cached(cf)
                    from_cache = html is not None
                    if html is None:
                        html = _fetch_detail_html(context, lst.detail_url, referer=url)
                        if html is not None:
                            store_cached(cf, html)

                    if html is None:
                        progress(
                            f"[profile] {i:>4}/{total}: {lst.name!r} - FETCH FAILED"
                        )
                        yield Lead(
                            company_name=lst.name,
                            country=lst.country,
                            booth_number=lst.booth,
                            company_profile_url=lst.detail_url,
                            notes="detail fetch failed",
                        )
                    else:
                        pp = context.new_page()
                        try:
                            pp.set_content(html, wait_until="domcontentloaded")
                            lead = _parse_detail(pp, lst, html)
                        finally:
                            pp.close()
                        tag = "cache" if from_cache else "fresh"
                        progress(
                            f"[profile] {i:>4}/{total}: {lst.name!r} ({tag}) "
                            f"email={lead.company_email or '-'} "
                            f"website={lead.company_website or '-'}"
                        )
                        yield lead

                    if not from_cache:
                        jittered_sleep()
            finally:
                browser.close()
