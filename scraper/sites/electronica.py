"""Electronica (electronica.de) scraper.

URL: https://exhibitors.electronica.de/exhibitor-portal/{year}/

The listing is server-side rendered but uses POST-based pagination
(forms named `paging_1` / `paging_2`). We drive it via Playwright by
clicking the "Next" button until it disappears, collecting detail-page
links from each page.

Per-exhibitor detail pages are similarly server-side. They contain a
"Contact" block with the company's main email (mailto:), phone (tel:),
website, and postal address — directly visible, no auth required.
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
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from scraper.core.cache import cache_path, load_cached, store_cached
from scraper.core.politeness import USER_AGENT, jittered_sleep
from scraper.core.types import Lead, ProgressFn, Scraper, _NULL_PROGRESS

NEXT_PAGE_SELECTOR = "[aria-label='Next page']"
DETAIL_LINK_SELECTOR = "a[href*='exhibitordetails']"
SLUG_FROM_DETAIL_PATTERN = re.compile(r"/exhibitordetails/([^/?#]+)/")

# Hosts that are part of electronica's own infra or third-party tracking;
# exclude when guessing the company's website link.
SKIP_HOSTS = {
    "electronica.de",
    "messe-muenchen.de",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "facebook.com",
    "instagram.com",
    "xing.com",
    "google.com",
    "bing.com",
    "adobe.com",
    "usercentrics.eu",
}


@dataclass(frozen=True)
class _Listing:
    name: str
    detail_url: str
    slug: str


def _extract_slug(url: str) -> str:
    m = SLUG_FROM_DETAIL_PATTERN.search(url)
    return m.group(1) if m else ""


def _extract_listings(page: Page) -> list[_Listing]:
    """Pull (name, detail_url, slug) tuples from anchors on the current
    listing page. Dedup by detail-URL slug."""
    out: list[_Listing] = []
    seen: set[str] = set()
    for a in page.locator(DETAIL_LINK_SELECTOR).all():
        href = a.get_attribute("href") or ""
        slug = _extract_slug(href)
        if not slug or slug in seen:
            continue
        text = (a.inner_text() or "").strip()
        if not text:
            continue
        seen.add(slug)
        out.append(_Listing(name=text, detail_url=href, slug=slug))
    return out


def _dismiss_cookie_banner(page: Page) -> None:
    """The usercentrics cookie banner overlay intercepts pointer events
    and blocks pagination clicks. Hide it via JS (clicking 'Accept'
    isn't reliable across runs because the shadow-DOM markup differs)."""
    try:
        page.evaluate(
            """() => {
                const el = document.getElementById('usercentrics-cmp-ui');
                if (el) el.remove();
                // Also hide any other top-z-index overlays
                document.querySelectorAll('aside[id*="usercentrics"], div[id*="usercentrics"]')
                    .forEach(e => e.remove());
            }"""
        )
    except Exception:
        pass


def _click_next(page: Page) -> bool:
    """Click the 'Next page' AJAX button. Returns True if a click fired.

    The button is an <input aria-label='Next page'> whose onclick fires
    `useAjaxChangeforElement(...)` to swap #jl_contentArea in place
    (no URL change). We dispatch the click via JS to bypass any
    pointer-event-intercepting overlays (cookie banners, etc).
    """
    candidate = page.locator(NEXT_PAGE_SELECTOR)
    if candidate.count() == 0:
        return False
    for i in range(candidate.count()):
        el = candidate.nth(i)
        try:
            if not el.is_visible() or not el.is_enabled():
                continue
            # JS .click() bypasses pointer-event overlay interception
            el.evaluate("e => e.click()")
            return True
        except Exception:
            continue
    return False


def _walk_listing(
    page: Page,
    listing_url: str,
    max_iterations: int,
    progress: ProgressFn,
) -> list[_Listing]:
    progress(f"[listing] opening {listing_url}")
    page.goto(listing_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_selector(DETAIL_LINK_SELECTOR, timeout=30_000)
    except PlaywrightTimeoutError:
        progress("[listing] no detail anchors appeared")
        return []
    _dismiss_cookie_banner(page)

    all_listings: dict[str, _Listing] = {}
    for i in range(max_iterations):
        current = _extract_listings(page)
        for lst in current:
            all_listings.setdefault(lst.slug, lst)
        progress(
            f"[listing] iter {i:>3}: {len(all_listings)} unique exhibitors collected"
        )
        first_slug_before = current[0].slug if current else ""

        if not _click_next(page):
            progress("[listing] no 'Next page' button - assumed exhausted")
            break

        # Pagination is AJAX (the onclick swaps #jl_contentArea via JS).
        # Wait until the FIRST visible card's slug actually changes,
        # rather than for a selector that's already present.
        content_changed = False
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            time.sleep(0.4)
            new = _extract_listings(page)
            if new and (not first_slug_before or new[0].slug != first_slug_before):
                content_changed = True
                break
        if not content_changed:
            progress(
                f"[listing] AJAX content didn't change after Next - assumed exhausted"
            )
            break

        jittered_sleep(base=1.0, jitter=0.5)

    return list(all_listings.values())


def _is_skippable_host(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


def _pick_website(page: Page) -> str:
    """First http(s) anchor in the page that isn't electronica or social."""
    for a in page.locator("a[href^='http']").all():
        href = a.get_attribute("href") or ""
        try:
            host = urlparse(href).netloc
        except ValueError:
            continue
        if host and not _is_skippable_host(host):
            return href
    return ""


def _extract_address(page: Page, name: str, phone_href: str) -> str:
    """Heuristic: take the body-text line immediately above the phone line
    inside the Contact block. Looks brittle but is what the page gives us
    without microdata."""
    try:
        body = page.locator("body").inner_text()
    except Exception:
        return ""
    if not phone_href:
        return ""
    phone_digits = re.sub(r"\D", "", phone_href.replace("tel:", ""))
    if not phone_digits:
        return ""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.sub(r"\D", "", ln) == phone_digits and i > 0:
            candidate = lines[i - 1]
            # Skip obvious non-address lines (the company name itself, headings)
            if candidate.lower() == name.lower():
                continue
            if candidate in {"Contact", "Other contact", "Contact sales"}:
                continue
            return candidate
    return ""


def _parse_detail(page: Page, name_fallback: str) -> Lead:
    # Name: try the most prominent heading on the page; fall back to listing.
    name = ""
    for sel in ["h1", "h2"]:
        loc = page.locator(sel)
        if loc.count():
            name = (loc.first.inner_text() or "").strip()
            if name:
                break
    if not name:
        name = name_fallback

    # Email + phone via first mailto/tel anchor (these are the company-main ones
    # since they appear at the top of the Contact block before named contacts).
    email_loc = page.locator("a[href^='mailto:']")
    email = ""
    if email_loc.count():
        email = (email_loc.first.get_attribute("href") or "").removeprefix("mailto:").strip()

    phone_loc = page.locator("a[href^='tel:']")
    phone = ""
    phone_href = ""
    if phone_loc.count():
        phone_href = phone_loc.first.get_attribute("href") or ""
        phone = (phone_loc.first.inner_text() or "").strip() or phone_href.removeprefix("tel:").strip()

    website = _pick_website(page)
    address = _extract_address(page, name, phone_href)

    # Booth (e.g. "C6.121") appears as a prominent inline tag near the top.
    # No reliable selector — look for the pattern in the first few hundred chars.
    booth = ""
    try:
        head_text = page.locator("body").inner_text()[:600]
        m = re.search(r"\b[A-Z]\d{1,2}[A-Z]?\.\d{1,4}\b", head_text)
        if m:
            booth = m.group(0)
    except Exception:
        pass

    return Lead(
        company_name=name,
        country="",  # not reliably structured on the page; address often has it
        booth_number=booth,
        company_email=email,
        company_phone=phone,
        company_website=website,
        address=address,
        company_profile_url=page.url,
        email_source="dom" if email else "not_found",
        email_confidence="medium" if email else "",
    )


def _fetch_detail_html(
    context: BrowserContext, detail_url: str, *, referer: str | None
) -> str | None:
    page = context.new_page()
    try:
        r = page.goto(
            detail_url,
            wait_until="domcontentloaded",
            timeout=45_000,
            referer=referer,
        )
        if r is None or r.status >= 400:
            return None
        page.wait_for_timeout(300)
        return page.content()
    except PlaywrightTimeoutError:
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


class ElectronicaScraper(Scraper):
    site_id = "electronica"
    site_label = "Electronica"

    @classmethod
    def matches(cls, url: str) -> bool:
        return "exhibitors.electronica.de" in url

    def scrape(
        self,
        url: str,
        *,
        cache_dir: Path | None = None,
        max_listing_iterations: int | None = None,
        max_profiles: int | None = None,
        progress: ProgressFn = _NULL_PROGRESS,
    ) -> Iterator[Lead]:
        listing_cap = (
            max_listing_iterations if max_listing_iterations is not None else 400
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            try:
                listing_page = context.new_page()
                listings = _walk_listing(listing_page, url, listing_cap, progress)
                listing_page.close()

                progress(f"[listing] returned {len(listings)} exhibitors")
                if max_profiles is not None and len(listings) > max_profiles:
                    listings = listings[:max_profiles]
                    progress(
                        f"[listing] capped to {len(listings)} profiles for this run"
                    )

                total = len(listings)
                for i, lst in enumerate(listings, 1):
                    cf = cache_path(cache_dir, self.site_id, lst.slug)
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
                            company_profile_url=lst.detail_url,
                            notes="detail fetch failed",
                        )
                    else:
                        # Re-render in a page so locator queries work; rather
                        # than re-parsing the cached HTML string, we load it
                        # into a new page via set_content for selector queries.
                        parse_page = context.new_page()
                        try:
                            parse_page.set_content(html, wait_until="domcontentloaded")
                            # set_content gives us about: as URL; we want the real one
                            lead = _parse_detail(parse_page, name_fallback=lst.name)
                            # Override profile_url with the actual detail URL since
                            # set_content makes page.url == 'about:blank'
                            lead = Lead(
                                company_name=lead.company_name or lst.name,
                                country=lead.country,
                                booth_number=lead.booth_number,
                                company_email=lead.company_email,
                                company_phone=lead.company_phone,
                                company_website=lead.company_website,
                                address=lead.address,
                                company_profile_url=lst.detail_url,
                                email_source=lead.email_source,
                                email_confidence=lead.email_confidence,
                                notes=lead.notes,
                            )
                        finally:
                            parse_page.close()

                        tag = "cache" if from_cache else "fresh"
                        progress(
                            f"[profile] {i:>4}/{total}: {lst.name!r} ({tag}) "
                            f"email={lead.company_email or '-'} "
                            f"phone={lead.company_phone or '-'}"
                        )
                        yield lead

                    if not from_cache:
                        jittered_sleep()
            finally:
                browser.close()
