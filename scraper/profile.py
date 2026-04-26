"""Fetch a CPHI company-profile page and extract its JSON-LD Organization data.

Each company on cphi-online.com has a /company/{slug}/ page with a
<script type="application/ld+json"> block containing structured data
(name, telephone, email, postal address). Parsing that is far more
reliable than scraping the rendered DOM.

This module exposes two things, deliberately separated:
- fetch_profile_html() does the network call only
- parse_profile_html() does the JSON-LD extraction only
The orchestrator (scraper.run) handles caching and politeness on top.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from playwright.sync_api import BrowserContext, TimeoutError as PlaywrightTimeoutError

JSONLD_PATTERN = re.compile(
    r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
    re.DOTALL,
)
PROFILE_URL_TEMPLATE = "https://www.cphi-online.com/company/{slug}/"

# Pattern for the canonical /company/{slug}/ link on the exhibitor detail page
# (the "View company profile" button).
EXHIBITOR_PROFILE_HREF_PATTERN = re.compile(r'href="/company/([^/?#"]+)/"')


@dataclass(frozen=True)
class Profile:
    slug: str
    profile_url: str
    name: str
    email: str
    phone: str
    address: str
    address_country: str
    found: bool  # True if Organization JSON-LD was present


def profile_url_for(slug: str) -> str:
    return PROFILE_URL_TEMPLATE.format(slug=slug)


def _format_address(addr: dict | None) -> tuple[str, str]:
    """Return (formatted_address, country_code)."""
    if not isinstance(addr, dict):
        return "", ""
    parts = [
        addr.get("streetAddress"),
        addr.get("addressLocality"),
        addr.get("addressRegion"),
        addr.get("postalCode"),
        addr.get("addressCountry"),
    ]
    formatted = ", ".join(p for p in parts if p)
    return formatted, addr.get("addressCountry") or ""


def _extract_organization(html: str) -> dict | None:
    for match in JSONLD_PATTERN.finditer(html):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        graph = data.get("@graph") if isinstance(data, dict) else None
        nodes = graph if isinstance(graph, list) else [data]
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "Organization":
                return node
    return None


def parse_profile_html(html: str, slug: str) -> Profile:
    org = _extract_organization(html)
    profile_url = profile_url_for(slug)
    if not org:
        return Profile(
            slug=slug,
            profile_url=profile_url,
            name="",
            email="",
            phone="",
            address="",
            address_country="",
            found=False,
        )
    formatted_addr, country = _format_address(org.get("address"))
    return Profile(
        slug=slug,
        profile_url=profile_url,
        name=(org.get("name") or "").strip(),
        email=(org.get("email") or "").strip(),
        phone=(org.get("telephone") or "").strip(),
        address=formatted_addr,
        address_country=country,
        found=True,
    )


def find_slug_via_exhibitor_page(
    context: BrowserContext,
    exhibitor_url: str,
    *,
    referer: str | None = None,
    timeout_ms: int = 45_000,
) -> str | None:
    """Visit the event-scoped exhibitor page and pull the canonical slug
    from the 'View company profile' link. Used as a fallback when our
    name-derived slug guess 404s."""
    if not exhibitor_url:
        return None
    page = context.new_page()
    try:
        response = page.goto(
            exhibitor_url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
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


def fetch_profile_html(
    context: BrowserContext,
    slug: str,
    *,
    referer: str | None = None,
    timeout_ms: int = 45_000,
) -> str | None:
    """Network-only fetch of /company/{slug}/. Returns HTML or None on miss."""
    if not slug:
        return None
    page = context.new_page()
    try:
        response = page.goto(
            profile_url_for(slug),
            wait_until="domcontentloaded",
            timeout=timeout_ms,
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
