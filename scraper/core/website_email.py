"""Gap-fill missing emails by scraping the company's own website.

Some exhibition platforms (Space Tech Expo, parts of Electronica) don't
expose exhibitor email addresses at all. But they DO give us the company's
own website URL. This module hits that website's homepage + a few common
contact paths and extracts the most likely company email.

Used as a post-scrape enrichment pass in `scraper.run`. Each Lead with
empty email + non-empty website gets passed through `find_email_for_website`.

Caching: per-domain (cache/_websites/{domain}.html), so the same company
attending multiple shows is only fetched once.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, TimeoutError as PlaywrightTimeoutError

from scraper.core.cache import cache_path, load_cached, store_cached

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Contact-page paths to try if homepage doesn't surface an email.
CONTACT_PATHS = [
    "/contact",
    "/contact-us",
    "/contact_us",
    "/contactus",
    "/contacts",
    "/about",
    "/about-us",
    "/aboutus",
    "/imprint",  # English
    "/impressum",  # German legal-imprint page; required by German law
    "/kontakt",
    "/get-in-touch",
]

# Local-parts that are almost always automation, legal, abuse handlers,
# or placeholder text in form HTML.
SKIP_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "no_reply",
    "privacy", "datenschutz", "dpo", "gdpr",
    "abuse", "postmaster", "webmaster", "hostmaster", "mailer-daemon",
    "spam", "phishing", "security",
    # Form placeholders — "your@email.com" / "name@example.com" patterns
    "your", "yourname", "yourcompany", "name", "user", "username",
    "you", "email", "e-mail", "youremail",
}

# Domains that show up as junk hits (placeholders, infra, free providers
# unlikely to be a corporate contact).
SKIP_DOMAINS = {
    "example.com", "example.org", "example.net",
    "test.com", "domain.com", "yoursite.com",
    "yourcompany.com", "yourdomain.com", "mycompany.com",
    "email.com",  # paired with form-placeholder local parts like "your"
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk",
    "hotmail.com", "outlook.com", "live.com", "icloud.com",
    "aol.com", "gmx.de", "gmx.com", "web.de",
    "sentry.io", "sentry-next.wixpress.com",
    "wordpress.com", "wix.com", "squarespace.com",
    "cloudflare.com", "cloudflare-dns.com",
}

# Local-parts to prefer when ranking. The ordering matters — earlier =
# better, used as a tiebreak in scoring.
PREFERRED_LOCAL_PARTS = (
    "info", "contact", "sales", "hello", "enquiries", "enquiry",
    "kontakt", "office", "mail", "general", "marketing",
)


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _is_junk_email(em: str) -> bool:
    em = em.lower()
    local, _, domain = em.partition("@")
    if not domain:
        return True
    # Strip trailing punctuation that may have leaked from regex
    local = local.rstrip(".")
    domain = domain.rstrip(".")
    if not local or "." not in domain:
        return True
    # Local-part filters
    if local in SKIP_LOCAL_PARTS:
        return True
    # Hex-looking local parts (analytics tokens, image hashes that look
    # like "abc123def456@somecdn")
    if len(local) > 16 and re.fullmatch(r"[0-9a-f]{16,}", local):
        return True
    # Domain filters
    base = _strip_www(domain)
    if base in SKIP_DOMAINS:
        return True
    # Image / asset extensions in the email (e.g. "logo@2x.png@host")
    if any(em.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico")):
        return True
    return False


def _score_email(em: str, target_domain: str) -> int:
    """Higher = better. Same-domain matches beat preferred-prefix matches."""
    em = em.lower()
    local, _, domain = em.partition("@")
    base = _strip_www(domain)
    target = _strip_www(target_domain)
    score = 0
    # Strongest signal: email is on the same domain as the company website
    if target and (base == target or base.endswith("." + target) or target.endswith("." + base)):
        score += 100
    # Prefer generic outreach addresses over individual ones
    if local in PREFERRED_LOCAL_PARTS:
        score += 50
    # Tiny tiebreak: shorter local parts feel more "official"
    score -= len(local)
    return score


def _fetch_text(context: BrowserContext, url: str, timeout_ms: int = 12_000) -> tuple[str, str]:
    """Return (html, body_text). Empty strings on any failure.

    External company websites do all kinds of weird things — JS-triggered
    redirects, geo-blocking, cookie-wall navigations, 30s spinners. Catch
    everything here so one bad site doesn't kill the gap-fill pass.
    """
    page = context.new_page()
    try:
        try:
            r = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            return "", ""
        if r is None or r.status >= 400:
            return "", ""
        try:
            page.wait_for_timeout(300)
            html = page.content()
        except Exception:
            return "", ""
        try:
            body = page.locator("body").inner_text()
        except Exception:
            body = ""
        return html, body
    finally:
        try:
            page.close()
        except Exception:
            pass


def _candidate_emails(text: str) -> list[str]:
    """De-duped emails extracted from the input text, junk filtered out."""
    seen: list[str] = []
    seen_lower: set[str] = set()
    for em in EMAIL_RE.findall(text):
        norm = em.lower().rstrip(".")
        if norm in seen_lower or _is_junk_email(norm):
            continue
        seen_lower.add(norm)
        seen.append(norm)
    return seen


def _pick_best(emails: list[str], target_domain: str) -> str:
    if not emails:
        return ""
    return max(emails, key=lambda em: _score_email(em, target_domain))


def find_email_for_website(
    context: BrowserContext,
    website_url: str,
    *,
    cache_dir: Path | None = None,
    progress: Callable[[str], None] = lambda _s: None,
) -> str:
    """Find a company email by scraping its public website.

    Strategy:
      1. Look up by-domain cache; return immediately on hit (even if the
         hit is the empty string — that means we already tried and found
         nothing).
      2. Fetch homepage, regex emails, score, take best on-domain candidate.
      3. If no acceptable email on homepage, try common /contact /about
         /impressum paths sequentially, stop at first hit.
      4. Cache the result (empty or not).
    """
    if not website_url:
        return ""
    domain = _domain_of(website_url)
    if not domain:
        return ""

    cache_key = domain.replace(":", "_")
    cf = cache_path(cache_dir, "_websites", cache_key)
    cached = load_cached(cf)
    if cached is not None:
        return cached.strip()

    scheme = urlparse(website_url).scheme or "https"
    base = f"{scheme}://{domain}"
    candidates_to_try = [website_url]
    for path in CONTACT_PATHS:
        url = base + path
        if url not in candidates_to_try:
            candidates_to_try.append(url)

    found = ""
    for try_url in candidates_to_try:
        html, body = _fetch_text(context, try_url)
        if not html:
            continue
        emails = _candidate_emails(body + "\n" + html)
        if not emails:
            continue
        best = _pick_best(emails, target_domain=domain)
        if best:
            found = best
            progress(f"  [gapfill] {domain}: {best}  (via {try_url})")
            break

    if cf is not None:
        store_cached(cf, found)
    return found
