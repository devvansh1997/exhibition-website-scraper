"""Scrape an exhibition platform's exhibitor listing page.

v0.1: targets CPHI-style listings (e.g. https://exhibitors.cphi.com/cpww26/).
The listing page is JS-rendered. Each exhibitor is a `.exhibitor` card
containing name, country, booth, and a 'View profile' link to the detail
page on cphi-online.com. Loops 'Show more results' until exhausted.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

SHOW_MORE_TEXT_PATTERN = re.compile(r"show\s+more", re.IGNORECASE)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Exhibitor:
    name: str
    country: str
    booth: str
    detail_url: str


def _jittered_sleep(base: float = 1.5, jitter: float = 0.7) -> None:
    time.sleep(base + random.uniform(0, jitter))


def _safe_text(locator: Locator) -> str:
    if locator.count() == 0:
        return ""
    return (locator.first.inner_text() or "").strip()


def _safe_attr(locator: Locator, attr: str) -> str:
    if locator.count() == 0:
        return ""
    return (locator.first.get_attribute(attr) or "").strip()


def _extract_card(card: Locator) -> Exhibitor | None:
    name = _safe_text(card.locator(".exhibitor__title h3"))
    if not name:
        return None
    country = _safe_text(card.locator(".exhibitor__country"))
    booth = _safe_text(card.locator(".exhibitor__h-place .m-tag__txt"))
    # "View profile" anchor — distinct from the logo image and the "Contact Company" js link.
    detail_url = _safe_attr(
        card.locator("a.btn-outline-secondary[href*='exhibitor']"),
        "href",
    )
    return Exhibitor(name=name, country=country, booth=booth, detail_url=detail_url)


def _extract_visible_exhibitors(page: Page) -> list[Exhibitor]:
    cards = page.locator("div.exhibitor").all()
    results: list[Exhibitor] = []
    seen: set[str] = set()
    for card in cards:
        ex = _extract_card(card)
        if ex is None:
            continue
        # Dedup by detail_url when present, otherwise by name.
        key = ex.detail_url or ex.name
        if key in seen:
            continue
        seen.add(key)
        results.append(ex)
    return results


def _click_show_more(page: Page) -> bool:
    """Click 'Show more results'. Returns True if a click happened."""
    candidate = page.locator("button, a, [role='button']").filter(has_text=SHOW_MORE_TEXT_PATTERN)
    if candidate.count() == 0:
        return False
    try:
        candidate.first.scroll_into_view_if_needed(timeout=3_000)
        candidate.first.click(timeout=5_000)
        return True
    except PlaywrightTimeoutError:
        return False


def scrape_listing(url: str, max_iterations: int = 200) -> list[Exhibitor]:
    """Scrape the listing at `url`, expanding 'Show more' until exhausted."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        print(f"[listing] opening {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        try:
            page.wait_for_selector("div.exhibitor", timeout=30_000)
        except PlaywrightTimeoutError:
            print("[listing] no exhibitor cards appeared within 30s")
            browser.close()
            return []

        for i in range(max_iterations):
            current_count = page.locator("div.exhibitor").count()
            print(f"[listing] iter {i:>3}: {current_count} cards in DOM")

            if not _click_show_more(page):
                print("[listing] no 'Show more' control found - assumed exhausted")
                break

            _jittered_sleep()
            # Wait for the card count to grow (max 15s), instead of networkidle
            # which never fires on this SPA.
            grew = False
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if page.locator("div.exhibitor").count() > current_count:
                    grew = True
                    break
                time.sleep(0.3)
            if not grew:
                print(f"[listing] count plateaued at {current_count} - exhausted")
                break

        results = _extract_visible_exhibitors(page)
        browser.close()
        return results
