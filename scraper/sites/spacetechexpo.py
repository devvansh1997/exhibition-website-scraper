"""Space Tech Expo Europe (spacetechexpo-europe.com) scraper.

URL: https://www.spacetechexpo-europe.com/exhibitor-list/

Server-side HTML on a Salesforce Experience Cloud backend. The full
exhibitor list (~500+) is rendered into the page in one shot — no
pagination. Each card is a div.exhibitor-slide with the company name,
booth, and product categories visible inline.

Detail pages live at /exhibitor-list/exhibitor/?boothid=<sf_id> and
contain a Contact block with Website + Address (and sometimes a
mailto: link), plus a description and categories.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

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

ORIGIN = "https://www.spacetechexpo-europe.com"
BOOTHID_PATTERN = re.compile(r"boothid=([A-Za-z0-9]+)")

SKIP_HOSTS = {
    # Show + parent infra
    "spacetechexpo-europe.com",
    "spacetechexpo.com",  # US sister show
    "smartershows.com",  # parent organiser
    "mapyourshow.com",  # floor-plan tool linked from every detail
    "visitcloud.com",  # registration tool linked from every detail
    "register.visitcloud.com",
    "translate.google.com",
    # Sister shows in the Smarter Shows family — all linked from every
    # detail page's footer. The real company URL appears further down
    # the link list, so each one of these has to be skipped explicitly.
    "foam-expo.com",
    "foam-expo-europe.com",
    "ceramicsexpousa.com",
    "adhesivesandbondingexpo.com",
    "adhesivesandbondingexpo-mexico.com",
    "adhesivesandbondingexpo-europe.com",
    "thermalmanagementexpo.com",
    # Social / search
    "linkedin.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "facebook.com",
    "instagram.com",
    "google.com",
}

# Skip these email domains entirely — they are the show family's own
# contacts, never the exhibitor's. STE / its sister shows render their
# global emails on every detail page (no per-exhibitor email is exposed).
SKIP_EMAIL_DOMAINS = {
    "spacetechexpo-europe.com",
    "spacetechexpo.com",
    "smartershows.com",
    "foam-expo.com",
    "foam-expo-europe.com",
    "ceramicsexpousa.com",
    "adhesivesandbondingexpo.com",
    "adhesivesandbondingexpo-mexico.com",
    "adhesivesandbondingexpo-europe.com",
    "thermalmanagementexpo.com",
    "example.com",  # template placeholder seen on every page
}


@dataclass(frozen=True)
class _Listing:
    boothid: str
    name: str
    booth: str
    categories: str
    detail_url: str


def _safe_text(loc: Locator) -> str:
    if loc.count() == 0:
        return ""
    return (loc.first.inner_text() or "").strip()


def _extract_cards(page: Page) -> list[_Listing]:
    out: list[_Listing] = []
    seen: set[str] = set()
    for card in page.locator("div.exhibitor-slide").all():
        boothid = (card.get_attribute("id") or "").strip()
        if not boothid or boothid in seen:
            continue
        name = _safe_text(card.locator(".exhibitor-name"))
        if not name:
            continue
        booth = _safe_text(card.locator(".exhibitor-booth")).replace(
            "Booth Number:", ""
        ).strip()
        cats = _safe_text(card.locator(".exhibitor-slide__cats")).strip()
        # Normalize category separator: "A | B | C" -> "A; B; C"
        cats = re.sub(r"\s*\|\s*", "; ", cats)
        detail_url = urljoin(ORIGIN, f"/exhibitor-list/exhibitor/?boothid={boothid}")
        seen.add(boothid)
        out.append(
            _Listing(
                boothid=boothid,
                name=name,
                booth=booth,
                categories=cats,
                detail_url=detail_url,
            )
        )
    return out


def _is_skippable_host(host: str) -> bool:
    host = host.lower()
    return any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


def _pick_company_website(page: Page) -> str:
    for a in page.locator("a[href^='http']").all():
        href = a.get_attribute("href") or ""
        m = re.match(r"https?://([^/]+)", href)
        if not m:
            continue
        host = m.group(1)
        if not _is_skippable_host(host):
            return href
    return ""


def _extract_address(body_text: str) -> str:
    """Pull the company address out of the body text. The page renders
    it as a labelled block:
        Address:
        <line 1>,
        <line 2>,
        ...
        Venue Address    <- next section, stop here
    """
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    try:
        i = lines.index("Address:")
    except ValueError:
        return ""
    parts: list[str] = []
    for ln in lines[i + 1:]:
        if ln in {"Venue Address", "Phone:", "Website", "Contact"}:
            break
        # Avoid sucking in unrelated trailing content
        if ln.lower().startswith(("phone:", "website:")):
            break
        parts.append(ln.rstrip(",").rstrip("."))
        if len(parts) >= 6:
            break
    return ", ".join(p for p in parts if p)


def _pick_company_email(page: Page) -> str:
    """First mailto that isn't the show's own contact email."""
    for a in page.locator("a[href^='mailto:']").all():
        href = (a.get_attribute("href") or "").removeprefix("mailto:").strip()
        if "@" not in href:
            continue
        domain = href.rsplit("@", 1)[-1].lower()
        if any(domain == d or domain.endswith("." + d) for d in SKIP_EMAIL_DOMAINS):
            continue
        return href
    return ""


def _parse_detail(page: Page, listing: _Listing) -> Lead:
    email = _pick_company_email(page)

    # Phone (rarely populated on STE detail; tel: link)
    phone = ""
    phone_loc = page.locator("a[href^='tel:']")
    if phone_loc.count():
        phone_href = phone_loc.first.get_attribute("href") or ""
        phone = (phone_loc.first.inner_text() or "").strip() or phone_href.removeprefix("tel:").strip()

    website = _pick_company_website(page)

    body_text = ""
    try:
        body_text = page.locator("body").inner_text()
    except Exception:
        pass
    address = _extract_address(body_text)

    # Country from address: pick the last component that is purely letters
    # (with spaces), so we skip postcodes like "03431" and "H-1013".
    country = ""
    if address:
        for c in reversed([c.strip() for c in address.split(",")]):
            if c and c.replace(" ", "").isalpha():
                country = c
                break

    return Lead(
        company_name=listing.name,
        country=country,
        booth_number=listing.booth,
        company_email=email,
        company_phone=phone,
        company_website=website,
        address=address,
        company_profile_url=listing.detail_url,
        email_source="dom" if email else "not_found",
        email_confidence="medium" if email else "",
        notes=("categories: " + listing.categories) if listing.categories else "",
    )


import time as _time


def _wait_for_company_content(page: Page, timeout_s: float = 12.0) -> None:
    """STE detail pages are React-rendered. domcontentloaded fires
    before the company-specific block (booth, address, the actual
    company website link) is in the DOM — leaving us with just the
    page shell that has sister-show URLs in the footer. Poll body
    text for markers that the company content has rendered."""
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        try:
            body = page.locator("body").inner_text()
        except Exception:
            body = ""
        if "Booth number" in body or "Categories:" in body or "Back to Exhibitor List" in body:
            return
        _time.sleep(0.3)


def _fetch_detail_html(
    context: BrowserContext, url: str, *, referer: str | None
) -> str | None:
    page = context.new_page()
    try:
        r = page.goto(url, wait_until="domcontentloaded", timeout=45_000, referer=referer)
        if r is None or r.status >= 400:
            return None
        _wait_for_company_content(page)
        return page.content()
    except PlaywrightTimeoutError:
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


class SpaceTechExpoScraper(Scraper):
    site_id = "spacetechexpo"
    site_label = "Space Tech Expo Europe"

    @classmethod
    def matches(cls, url: str) -> bool:
        return "spacetechexpo-europe.com" in url

    def scrape(
        self,
        url: str,
        *,
        cache_dir: Path | None = None,
        max_listing_iterations: int | None = None,  # unused: single page
        max_profiles: int | None = None,
        progress: ProgressFn = _NULL_PROGRESS,
    ) -> Iterator[Lead]:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=USER_AGENT)
            try:
                progress(f"[listing] opening {url}")
                lp = context.new_page()
                lp.goto(url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    lp.wait_for_selector("div.exhibitor-slide", timeout=30_000)
                except PlaywrightTimeoutError:
                    progress("[listing] no exhibitor cards found")
                    lp.close()
                    return
                lp.wait_for_timeout(1500)  # let lazy-loaded cards settle
                cards = _extract_cards(lp)
                lp.close()
                progress(f"[listing] returned {len(cards)} exhibitors")

                if max_profiles is not None and len(cards) > max_profiles:
                    cards = cards[:max_profiles]
                    progress(f"[listing] capped to {len(cards)} profiles")

                total = len(cards)
                for i, lst in enumerate(cards, 1):
                    cf = cache_path(cache_dir, self.site_id, lst.boothid)
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
                            booth_number=lst.booth,
                            company_profile_url=lst.detail_url,
                            notes="detail fetch failed",
                        )
                    else:
                        pp = context.new_page()
                        try:
                            pp.set_content(html, wait_until="domcontentloaded")
                            lead = _parse_detail(pp, lst)
                        finally:
                            pp.close()
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
