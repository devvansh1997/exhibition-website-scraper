"""EuroTier (digital.eurotier.com) scraper.

URL: https://digital.eurotier.com/newfront/marketplace/exhibitors?pageNumber=1&limit=60

ExpoPlatform white-label running on a React/Material-UI frontend.
Pagination is clean URL params (?pageNumber=N&limit=60); server honours
limit roughly (we see 12-18 cards per page in practice).

Detail pages at /newfront/exhibitor/{slug}-{hash} expose:
  - Email (mailto: link in a 'COMPANY EMAIL' block) — real company emails
  - Address block (street, locality+postcode, country) in plain text
  - Website (text-rendered URL, sometimes as link)
  - Categories
No phone exposed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit, parse_qs, urlencode

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from scraper.core.cache import cache_path, load_cached, store_cached
from scraper.core.politeness import USER_AGENT, jittered_sleep
from scraper.core.types import Lead, ProgressFn, Scraper, _NULL_PROGRESS

ORIGIN = "https://digital.eurotier.com"
DETAIL_LINK_SELECTOR = "a[href*='/newfront/exhibitor/']"
SLUG_FROM_URL_RE = re.compile(r"/newfront/exhibitor/([^/?#]+)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

SKIP_HOSTS = {
    "eurotier.com",
    "digital.eurotier.com",
    "dlg.org",
    "dlg-markets.com",
    "expoplatform.com",
    "cloudfront.net",  # CDN host for images
    "ccm19.de",  # cookie consent provider, linked from every page
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "facebook.com",
    "instagram.com",
    "google.com",
}
SKIP_EMAIL_DOMAINS = SKIP_HOSTS | {"example.com"}


@dataclass(frozen=True)
class _Listing:
    slug: str
    detail_url: str


def _page_url(listing_url: str, page_number: int) -> str:
    """Replace pageNumber in the listing URL while preserving other params."""
    parts = urlsplit(listing_url)
    qs = parse_qs(parts.query)
    qs["pageNumber"] = [str(page_number)]
    qs.setdefault("limit", ["60"])
    new_qs = urlencode({k: v[0] for k, v in qs.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_qs, parts.fragment))


def _extract_listings(page: Page) -> list[_Listing]:
    out: list[_Listing] = []
    seen: set[str] = set()
    for a in page.locator(DETAIL_LINK_SELECTOR).all():
        href = a.get_attribute("href") or ""
        m = SLUG_FROM_URL_RE.search(href)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        if not href.startswith("http"):
            href = urljoin(ORIGIN, href)
        out.append(_Listing(slug=slug, detail_url=href))
    return out


def _walk_listing(
    context: BrowserContext,
    listing_url: str,
    max_iterations: int,
    progress: ProgressFn,
) -> list[_Listing]:
    all_listings: dict[str, _Listing] = {}
    for page_no in range(1, max_iterations + 1):
        url = _page_url(listing_url, page_no)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            try:
                # Wait briefly for hydration; if no anchors appear, treat as empty
                page.wait_for_selector(DETAIL_LINK_SELECTOR, timeout=8_000)
            except PlaywrightTimeoutError:
                progress(f"[listing] page {page_no}: empty - stopping")
                return list(all_listings.values())
            page.wait_for_timeout(2500)  # let React hydrate the rest of the list

            this_page = _extract_listings(page)
            if not this_page:
                progress(f"[listing] page {page_no}: 0 cards - stopping")
                return list(all_listings.values())

            new = 0
            for lst in this_page:
                if lst.slug not in all_listings:
                    all_listings[lst.slug] = lst
                    new += 1
            progress(
                f"[listing] page {page_no:>3}: {len(this_page)} cards "
                f"({new} new, {len(all_listings)} total)"
            )
            if new == 0:
                # Server returned a page we've already seen — exhausted
                progress(f"[listing] page {page_no}: all duplicates - stopping")
                return list(all_listings.values())
        finally:
            page.close()
        jittered_sleep(base=1.0, jitter=0.5)
    return list(all_listings.values())


def _is_skippable_host(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


def _pick_company_email(page: Page, html: str) -> str:
    # Prefer mailto: anchors (most reliable)
    for a in page.locator("a[href^='mailto:']").all():
        href = (a.get_attribute("href") or "").removeprefix("mailto:").strip()
        if "@" not in href:
            continue
        domain = href.rsplit("@", 1)[-1].lower()
        if any(domain == d or domain.endswith("." + d) for d in SKIP_EMAIL_DOMAINS):
            continue
        return href
    # Fallback: regex over body text
    try:
        body = page.locator("body").inner_text()
    except Exception:
        body = ""
    for em in EMAIL_RE.findall(body) or EMAIL_RE.findall(html):
        domain = em.rsplit("@", 1)[-1].lower()
        if any(domain == d or domain.endswith("." + d) for d in SKIP_EMAIL_DOMAINS):
            continue
        return em
    return ""


def _pick_company_website(page: Page, body_text: str) -> str:
    # First, anchored external link
    for a in page.locator("a[href^='http']").all():
        href = a.get_attribute("href") or ""
        try:
            host = urlparse(href).netloc
        except ValueError:
            continue
        if host and not _is_skippable_host(host):
            return href
    # Fallback: www.something in body text
    m = re.search(r"\bwww\.[A-Za-z0-9.-]+\.[A-Za-z]{2,}", body_text)
    if m:
        return "https://" + m.group(0)
    return ""


PHONE_RE = re.compile(r"^\+?[\d\s().\-/]{8,}$")


def _parse_address_block(body_text: str, name: str) -> tuple[str, str, str]:
    """Parse the contact block following the company name.

    The detail page renders, after the name:
       <street>
       <locality / postcode>
       <country>
       [<phone>]      ← sometimes present
       <website / categories / CTA buttons>

    Returns (address, country, phone).
    """
    if not body_text or not name:
        return "", "", ""
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    # Name often appears twice (breadcrumb + main heading) — address
    # follows the LAST occurrence.
    i = -1
    for idx, ln in enumerate(lines):
        if ln == name:
            i = idx
    if i < 0:
        return "", "", ""
    # CTA labels / nav items the React app renders below the address;
    # encountering one means we've walked past the contact block.
    STOP_TOKENS = {
        "meet", "message", "contact", "contact us", "book", "book a meeting",
        "share", "save", "favorite", "favourite", "add to favorites",
        "send", "send message", "follow", "request", "categories",
        "matchmaking information", "company email", "matchmaking",
    }
    addr_parts: list[str] = []
    country = ""
    phone = ""
    for ln in lines[i + 1:]:
        ln_lower = ln.lower().strip(".,")
        if ln_lower.startswith(("www.", "http")):
            break
        if "@" in ln:
            break
        if ln_lower in STOP_TOKENS:
            break
        if PHONE_RE.match(ln):
            phone = ln.strip()
            break
        addr_parts.append(ln.rstrip(",").rstrip("."))
        # Country is typically the last purely-alphabetic line of the address
        if ln.replace(" ", "").isalpha():
            country = ln
        if len(addr_parts) >= 5:
            break
    return ", ".join(addr_parts), country, phone


def _parse_detail(page: Page, html: str, listing: _Listing) -> Lead:
    # Name: first prominent heading; if not present, derive from slug
    name = ""
    for sel in ("h1", "h2", "h3"):
        loc = page.locator(sel)
        if loc.count():
            name = (loc.first.inner_text() or "").strip()
            if name:
                break
    if not name:
        # Slug like "1001-green-products-gmbh-4b8936e6" -> "1001 Green Products Gmbh"
        # (drop the trailing 8-hex disambiguator)
        bare = re.sub(r"-[0-9a-f]{8,}$", "", listing.slug)
        name = bare.replace("-", " ").title()

    try:
        body = page.locator("body").inner_text()
    except Exception:
        body = ""

    email = _pick_company_email(page, html)
    website = _pick_company_website(page, body)
    address, country, phone = _parse_address_block(body, name)

    return Lead(
        company_name=name,
        country=country,
        booth_number="",  # not reliably shown on Eurotier detail pages
        company_email=email,
        company_phone=phone,
        company_website=website,
        address=address,
        company_profile_url=listing.detail_url,
        email_source="dom" if email else "not_found",
        email_confidence="medium" if email else "",
    )


import time as _time


def _wait_for_react_content(page: Page, timeout_s: float = 15.0) -> None:
    """Eurotier's React app fetches exhibitor data asynchronously after
    DOMContentLoaded. Wait until the body text shows one of the section
    labels that only appear once data has rendered."""
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        try:
            body = page.locator("body").inner_text()
        except Exception:
            body = ""
        upper = body.upper()
        if "MATCHMAKING" in upper or "CATEGORIES" in upper or "COMPANY EMAIL" in upper:
            return
        _time.sleep(0.4)


def _fetch_detail_html(
    context: BrowserContext, url: str, *, referer: str | None
) -> str | None:
    page = context.new_page()
    try:
        r = page.goto(url, wait_until="domcontentloaded", timeout=45_000, referer=referer)
        if r is None or r.status >= 400:
            return None
        _wait_for_react_content(page)
        return page.content()
    except PlaywrightTimeoutError:
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


class EuroTierScraper(Scraper):
    site_id = "eurotier"
    site_label = "EuroTier"

    @classmethod
    def matches(cls, url: str) -> bool:
        return "digital.eurotier.com" in url

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
                progress(f"[listing] opening {url}")
                listings = _walk_listing(context, url, listing_cap, progress)
                progress(f"[listing] returned {len(listings)} exhibitors")
                if max_profiles is not None and len(listings) > max_profiles:
                    listings = listings[:max_profiles]
                    progress(f"[listing] capped to {len(listings)} profiles")

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
                            f"[profile] {i:>4}/{total}: {lst.slug!r} - FETCH FAILED"
                        )
                        yield Lead(
                            company_name=lst.slug.replace("-", " ").title(),
                            company_profile_url=lst.detail_url,
                            notes="detail fetch failed",
                        )
                    else:
                        pp = context.new_page()
                        try:
                            pp.set_content(html, wait_until="domcontentloaded")
                            lead = _parse_detail(pp, html, lst)
                        finally:
                            pp.close()
                        tag = "cache" if from_cache else "fresh"
                        progress(
                            f"[profile] {i:>4}/{total}: {lead.company_name!r} ({tag}) "
                            f"email={lead.company_email or '-'} "
                            f"website={lead.company_website or '-'}"
                        )
                        yield lead

                    if not from_cache:
                        jittered_sleep()
            finally:
                browser.close()
