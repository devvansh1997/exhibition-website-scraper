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

import json
import queue
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import unquote, urlparse

from playwright.sync_api import (
    BrowserContext,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from scraper.core.cache import cache_path, load_cached, store_cached
from scraper.core.politeness import USER_AGENT

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Phone extraction. We prefer intentional `tel:` links, then fall back to
# numbers that sit next to an explicit phone label. We deliberately do NOT
# scrape bare numbers — on a company website those are mostly VAT / reg /
# postcode / date noise, and a wrong phone is worse than a blank one for a
# client who's going to dial it.
TEL_HREF_RE = re.compile(r'href=["\']tel:([^"\']+)["\']', re.IGNORECASE)
PHONE_LABEL_RE = re.compile(
    r"(?:tel\.?|t\.?e\.?l|tél\.?|phone|telephone|telefon|téléphone"
    r"|fon|call\s+us|ph\.?)\s*[:.]?\s*"
    r"(\+?[0-9][0-9\s()./\-]{6,}[0-9])",
    re.IGNORECASE,
)

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


@dataclass(frozen=True)
class WebsiteContact:
    email: str = ""
    phone: str = ""


def _clean_phone(raw: str) -> str:
    """Light normalization only — collapse whitespace, fix the leading
    double-plus, trim trailing separators. Not full E.164 (that's a
    separate normalization pass); just make it dial-able and consistent."""
    s = re.sub(r"\s+", " ", raw.strip())
    s = re.sub(r"^\++", "+", s)  # "++45 ..." -> "+45 ..."
    s = s.strip(" .-/")
    return s


def _valid_phone_digits(cleaned: str) -> bool:
    digits = re.sub(r"\D", "", cleaned)
    # E.164 allows up to 15 digits; require at least 7 to skip short codes.
    return 7 <= len(digits) <= 15


def _extract_phone(html: str, body: str) -> str:
    """Best-effort company phone. tel: links first (intentional, clean),
    then numbers next to an explicit phone label. Bare numbers ignored."""
    for m in TEL_HREF_RE.findall(html):
        # tel: values are often URL-encoded (%2B -> +, %20 -> space, etc.)
        cleaned = _clean_phone(unquote(m))
        if _valid_phone_digits(cleaned):
            return cleaned
    for m in PHONE_LABEL_RE.findall(body):
        cleaned = _clean_phone(m)
        if _valid_phone_digits(cleaned):
            return cleaned
    return ""


# Homepage + at most this many contact-ish paths per site. Bounds the
# worst case (a site that publishes neither email nor phone) so the
# gap-fill pass stays within the GH Actions time budget.
_MAX_PAGES_PER_SITE = 6


def find_contact_for_website(
    context: BrowserContext,
    website_url: str,
    *,
    cache_dir: Path | None = None,
    progress: Callable[[str], None] = lambda _s: None,
) -> WebsiteContact:
    """Scrape a company's public website for BOTH email and phone.

    Visits the homepage, then common /contact /impressum-style paths,
    pulling email + phone from each page en route. Stops as soon as both
    are found (or the page cap is hit). Result cached per-domain as JSON.
    """
    if not website_url:
        return WebsiteContact()
    domain = _domain_of(website_url)
    if not domain:
        return WebsiteContact()

    cache_key = domain.replace(":", "_")
    cf = cache_path(cache_dir, "_websites", cache_key)
    cached = load_cached(cf)
    if cached is not None:
        return _contact_from_cache(cached)

    scheme = urlparse(website_url).scheme or "https"
    base = f"{scheme}://{domain}"
    candidates_to_try = [website_url]
    for path in CONTACT_PATHS:
        url = base + path
        if url not in candidates_to_try:
            candidates_to_try.append(url)

    found_email = ""
    found_phone = ""
    for try_url in candidates_to_try[:_MAX_PAGES_PER_SITE]:
        html, body = _fetch_text(context, try_url)
        if not html:
            continue
        if not found_email:
            emails = _candidate_emails(body + "\n" + html)
            best = _pick_best(emails, target_domain=domain) if emails else ""
            if best:
                found_email = best
        if not found_phone:
            ph = _extract_phone(html, body)
            if ph:
                found_phone = ph
        if found_email and found_phone:
            break

    if found_email or found_phone:
        progress(f"  [gapfill] {domain}: email={found_email or '-'} phone={found_phone or '-'}")

    contact = WebsiteContact(email=found_email, phone=found_phone)
    if cf is not None:
        store_cached(cf, json.dumps({"email": found_email, "phone": found_phone}))
    return contact


def _contact_from_cache(raw: str) -> WebsiteContact:
    """Parse a cached contact. Handles both the new JSON format and the
    legacy bare-email-string format (older caches)."""
    raw = raw.strip()
    if not raw:
        return WebsiteContact()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return WebsiteContact(
                email=(data.get("email") or "").strip(),
                phone=(data.get("phone") or "").strip(),
            )
    except json.JSONDecodeError:
        pass
    # Legacy cache: the whole file was just the email string.
    return WebsiteContact(email=raw)


def find_email_for_website(
    context: BrowserContext,
    website_url: str,
    *,
    cache_dir: Path | None = None,
    progress: Callable[[str], None] = lambda _s: None,
) -> str:
    """Backward-compatible wrapper returning just the email."""
    return find_contact_for_website(
        context, website_url, cache_dir=cache_dir, progress=progress
    ).email


# ---------------------------------------------------------------------------
# Concurrent gap-fill
# ---------------------------------------------------------------------------
#
# Each company website is on a different domain, so there's no per-domain
# rate-limiting concern from running many fetches in parallel — they go to
# different servers.
#
# We spin up N worker threads. Each owns its own Playwright + browser +
# context for its entire lifetime (Playwright's sync API isn't thread-safe
# across threads, so each thread needs its own instance). Tasks come via a
# queue; results go out via another. STOP sentinels terminate workers
# cleanly so their browsers shut down properly.

_STOP = object()


def _gap_fill_worker(
    task_q: "queue.Queue",
    result_q: "queue.Queue",
    cache_dir: Path | None,
    progress: Callable[[str], None],
) -> None:
    try:
        pw = sync_playwright().start()
    except Exception as e:
        # If we can't even start Playwright, surface it via the result queue
        # so the main thread doesn't deadlock waiting forever.
        while True:
            task = task_q.get()
            if task is _STOP:
                return
            idx, _url = task
            result_q.put((idx, WebsiteContact(), f"worker init failed: {e!r}"))
        return
    try:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=USER_AGENT)
    except Exception as e:
        try:
            pw.stop()
        except Exception:
            pass
        while True:
            task = task_q.get()
            if task is _STOP:
                return
            idx, _url = task
            result_q.put((idx, WebsiteContact(), f"worker init failed: {e!r}"))
        return

    try:
        while True:
            task = task_q.get()
            if task is _STOP:
                return
            idx, website_url = task
            try:
                contact = find_contact_for_website(
                    ctx, website_url, cache_dir=cache_dir, progress=progress
                )
                err = ""
            except Exception as e:
                contact = WebsiteContact()
                err = repr(e)
            result_q.put((idx, contact, err))
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def gap_fill_concurrent(
    items: Iterable[tuple[int, str]],
    *,
    n_workers: int = 8,
    cache_dir: Path | None = None,
    progress: Callable[[str], None] = lambda _s: None,
) -> Iterator[tuple[int, WebsiteContact, str]]:
    """Run gap-fill across `n_workers` threads. Yields
    (idx, WebsiteContact, err) in completion order (not input order)."""
    item_list = list(items)
    if not item_list:
        return
    n_workers = max(1, min(n_workers, len(item_list)))

    task_q: queue.Queue = queue.Queue()
    result_q: queue.Queue = queue.Queue()

    workers = []
    for _ in range(n_workers):
        t = threading.Thread(
            target=_gap_fill_worker,
            args=(task_q, result_q, cache_dir, progress),
            daemon=True,
        )
        t.start()
        workers.append(t)

    try:
        for item in item_list:
            task_q.put(item)
        for _ in range(len(item_list)):
            yield result_q.get()
    finally:
        for _ in workers:
            task_q.put(_STOP)
        for t in workers:
            t.join(timeout=15)
