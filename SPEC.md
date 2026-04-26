# Exhibition Website Scraper — Spec

Lead-generation scraper for an exhibition stall business. Given an exhibition's exhibitor-list URL, produce a CSV of companies + contact emails, tagged with exhibition metadata for downstream merge/analytics.

---

## 1. Goal & scope

**Goal:** Non-technical user (the business owner) pastes an exhibition URL into a GitHub Actions form and 30–60 minutes later receives a CSV of leads.

**In scope (v1):**
- One source platform: CPHI / `cphi-online.com` exhibitor lists (e.g. `https://exhibitors.cphi.com/cpww26/`)
- Output: per-run CSV with company name, website, country, generic contact email, and exhibition-tagging columns
- Polite scraping (rate-limited, normal browser UA, no proxies)
- Trigger: GitHub Actions `workflow_dispatch` with URL + metadata inputs
- Artifact: CSV uploaded to the workflow run; downloadable from GitHub UI

**Explicitly out of scope (v1):**
- Generalization across arbitrary exhibition platforms — start narrow, expand later
- Named decision-maker contacts (sales director, etc.) — would require Apollo/Hunter/LinkedIn
- Phone numbers, postal addresses — only email
- Notion-driven trigger — planned for v2
- Persistent DB — CSVs only, downstream merging handled in pandas/Excel
- Email validation via SMTP probing — only regex/format validation

---

## 2. Users & workflow

**Two personas:**
- **Operator (non-technical, runs the scraper)** — opens GitHub repo, clicks Actions tab, picks the workflow, fills in 4 fields, clicks Run, walks away. Comes back to download CSV.
- **Maintainer (you)** — owns the repo, adjusts parsers when a site changes, manages secrets.

**End-to-end flow:**
1. Operator goes to `Actions → Scrape Exhibition → Run workflow`
2. Fills in: `exhibition_url`, `exhibition_name`, `exhibition_year`, `industry`, `recipient_email`
3. Clicks Run. Workflow starts.
4. Workflow scrapes listing → detail pages → company websites → emails. Writes CSV.
5. **CSV emailed to `recipient_email` as an attachment** (via Gmail SMTP). Operator opens email, CSV is right there. They never have to touch GitHub again after step 3.
6. CSV is also uploaded as a GitHub workflow artifact (30-day retention) — backup in case email is lost.

---

## 3. Data model — CSV schema

One CSV per run. Filename: `output/{industry}_{exhibition_name}_{year}_{run_id}.csv`.

| Column | Type | Source | Notes |
|---|---|---|---|
| `exhibition_name` | string | input | e.g. "CPHI Milan" — constant per run |
| `exhibition_year` | int | input | e.g. 2026 — constant per run |
| `exhibition_url` | string | input | the source URL — constant per run |
| `industry` | string | input | e.g. "Pharma" — for cross-show analytics |
| `scraped_at` | ISO datetime | runtime | UTC, constant per run |
| `company_name` | string | listing page | required |
| `company_detail_url` | string | listing page | link to exhibitor page on the platform |
| `company_website` | string | detail page or Google fallback | may be empty |
| `country` | string | detail page or listing filter | may be empty |
| `booth_number` | string | detail page | may be empty |
| `product_categories` | string | detail page | semicolon-separated |
| `contact_email` | string | company website extraction | may be empty |
| `email_source` | enum | runtime | `detail_page` \| `company_website` \| `llm_extracted` \| `not_found` |
| `email_confidence` | enum | runtime | `high` (regex match on contact page) \| `medium` (LLM-extracted) \| `low` (homepage scrape) |
| `notes` | string | runtime | freeform — errors, fallbacks taken, ambiguities |

Tagging columns (`exhibition_name`, `year`, `industry`) are constants for the run — they exist on every row so a downstream `pandas.concat([...])` of multiple CSVs gives a single analyzable frame.

---

## 4. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ GitHub Actions runner (ubuntu-latest)                       │
│                                                             │
│  ┌────────────┐   ┌────────────┐   ┌──────────────────┐   │
│  │ Listing    │──▶│ Detail     │──▶│ Email Finder     │   │
│  │ Scraper    │   │ Scraper    │   │ (regex + LLM)    │   │
│  │ (Playwright)│   │ (Playwright)│   │ (httpx + Haiku) │   │
│  └────────────┘   └────────────┘   └──────────────────┘   │
│         │               │                    │              │
│         ▼               ▼                    ▼              │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  CSV Writer  →  output/*.csv  →  upload-artifact     │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  Cache: ./cache/ (HTML snapshots, gitignored, ephemeral)   │
└─────────────────────────────────────────────────────────────┘
```

**Stages run sequentially.** Each stage's output is checkpointed to disk so a retry resumes mid-run instead of starting over.

---

## 5. Tech stack

| Component | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Best Playwright support; pandas for CSV |
| Browser automation | Playwright (chromium, headless) | JS-rendered pages, evades trivial bot checks |
| HTTP (company sites) | httpx | Simple GET for static pages; fall back to Playwright if JS-rendered |
| LLM | Anthropic Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) | Cheap, fast, good at structured extraction |
| LLM SDK | `anthropic` | Official SDK, supports prompt caching |
| HTML parsing | `selectolax` or `BeautifulSoup` | selectolax is faster, BS4 is friendlier |
| CSV | `pandas` | Easy, plays well with downstream merging |
| Search fallback (find missing company websites) | DuckDuckGo HTML scrape (primary) → Brave Search API (plan B if DDG unreliable) | DDG is free no-key but historically flaky; Brave has a free tier (2000 queries/mo) and an API key, more reliable |
| Email delivery | `dawidd6/action-send-mail@v3` (Gmail SMTP) | Mature GH Action, supports attachments, just needs Gmail app password |
| Runtime | GitHub Actions, `ubuntu-latest` | Free, no install for operator |
| Secrets | GitHub Actions secrets | `ANTHROPIC_API_KEY`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, optionally `BRAVE_API_KEY` |

**Why Python over Node:** project is single-purpose ETL, pandas + Playwright Python is the path of least resistance. No frontend to share types with.

---

## 6. File layout

```
exhibition-website-scraper/
├── .github/workflows/
│   └── scrape.yml              # workflow_dispatch trigger
├── scraper/
│   ├── __init__.py
│   ├── run.py                  # entrypoint, orchestrates stages
│   ├── listing.py              # exhibition listing → [company, detail_url]
│   ├── detail.py               # detail page → website, country, etc.
│   ├── email_finder.py         # company website → email
│   ├── llm.py                  # Haiku wrapper for fallback extraction
│   ├── csv_writer.py
│   ├── politeness.py           # rate limiting, retries, UA rotation
│   └── config.py               # constants, env vars
├── tests/
│   ├── fixtures/               # saved HTML for offline tests
│   ├── test_listing.py
│   ├── test_detail.py
│   └── test_email_finder.py
├── output/                     # CSVs, gitignored
├── cache/                      # HTML snapshots, gitignored
├── pyproject.toml
├── .gitignore
├── README.md                   # operator-facing instructions
└── SPEC.md                     # this file
```

---

## 7. Scraping strategy — CPHI specifically

### 7.1 Listing page (`exhibitors.cphi.com/cpww{YY}/`)

- Page is JS-rendered. Spinner loads results in batches.
- Strategy: launch Playwright chromium, navigate, wait for results selector, loop:
  1. Scrape currently-visible exhibitor cards
  2. Click "Show more results" button
  3. Wait for new cards to load (network idle)
  4. Continue until button disappears or count stops growing
- Per card extract: `company_name`, `company_detail_url`
- **Throttle:** 1.5s between "show more" clicks; jittered.

### 7.2 Detail page (`cphi-online.com/company/{slug}/`)

- **Known issue:** initial probe returned HTTP 403. Likely user-agent or referer check. Mitigation:
  - Use Playwright (real browser, real headers) instead of httpx
  - Set referer = listing page
  - If still 403 after retry, log, continue, mark website as missing — Stage 3 will Google-search the company name as fallback
- Extract: `company_website`, `country`, `booth_number`, `product_categories`
- **Throttle:** 2s between detail page fetches; jittered.
- **Cache:** save raw HTML to `cache/detail/{slug}.html`. Re-runs skip cached pages unless `--no-cache`.

### 7.3 Company website → email

For each company with a website:
1. Fetch homepage with httpx (timeout 15s, follow redirects)
2. If response is mostly empty (likely JS-rendered SPA), retry with Playwright
3. Find candidate contact pages: links with text matching `/contact|about|reach.us|get.in.touch/i` or hrefs containing `contact|about`
4. Fetch the top 1–2 candidate contact pages
5. Extract emails:
   - **Pass 1 (regex):** `[\w.+-]+@[\w-]+\.[\w.-]+` over visible text + `mailto:` hrefs
   - **Pass 2 (LLM fallback):** if regex finds 0 emails or only junk, send up to 8KB of cleaned page text to Haiku with prompt: *"Extract the primary public contact email for this company. Return JSON: `{email: string|null, confidence: 'high'|'medium'|'low', reason: string}`. Reject obfuscated, placeholder, or unrelated emails."*
6. Filter junk:
   - Block: `noreply@`, `no-reply@`, `donotreply@`, `privacy@`, `dpo@`, `abuse@`, `webmaster@`
   - Block: emails on free providers (`@gmail.com`, `@yahoo.com`) — usually not a real corporate contact
   - Prefer: `info@`, `contact@`, `sales@`, `hello@`, `enquiries@`
7. If multiple valid emails: pick by preference order above.

**Cost expectation per run (1000 companies):**
- ~70% caught by regex → 0 LLM calls
- ~30% LLM fallback × ~5KB input × $1/MTok input = ~$1.50
- Output negligible
- **<$2 per run.**

### 7.4 Politeness rules (global)

- One worker at a time per domain (no parallel hits to the same site)
- Across-domain parallelism: max 4 concurrent workers
- User-Agent: a recent Chrome string; do not rotate per-request
- No proxy rotation
- Honor `robots.txt` for company sites (best-effort; many block scrapers but allow contact pages)
- Total runtime budget: 90 minutes; if exceeded, write partial CSV and exit cleanly

---

## 8. GitHub Actions workflow

`.github/workflows/scrape.yml` — `workflow_dispatch` only (no scheduled runs in v1).

**Inputs:**
- `exhibition_url` (required) — full URL of the exhibitor list
- `exhibition_name` (required) — e.g. "CPHI Milan"
- `exhibition_year` (required) — e.g. "2026"
- `industry` (required) — e.g. "Pharma"
- `recipient_email` (required) — where to email the CSV when done

**Steps:**
1. Checkout
2. Setup Python 3.12
3. Install deps (pip + `playwright install chromium`)
4. Run `python -m scraper.run` with inputs as env vars
5. `actions/upload-artifact@v4` — upload `output/*.csv` (retention: 30 days, backup)
6. `dawidd6/action-send-mail@v3` — email the CSV to `recipient_email`. Subject: `"Leads: {exhibition_name} {exhibition_year} ({row_count} companies)"`. Body: short summary stats (companies seen, with website, with email). Attachment: the CSV. Runs on `if: success()`.
7. (Optional) Send a separate failure email on `if: failure()` so the operator knows when something broke.

**Secrets required:**
- `ANTHROPIC_API_KEY` — for Haiku fallback extraction
- `GMAIL_USER` — sending Gmail address (e.g. `leads.scraper@gmail.com`)
- `GMAIL_APP_PASSWORD` — [Gmail app password](https://support.google.com/accounts/answer/185833), 16 chars, generated under Google Account → Security → 2-Step Verification → App passwords
- `BRAVE_API_KEY` — optional; only needed if DDG fallback proves unreliable and we switch search providers

**One-time setup (you, the maintainer):**
1. Create or pick a Gmail account to send from. Enable 2FA.
2. Generate a Gmail app password.
3. Add the 3 (or 4) secrets in repo Settings → Secrets and variables → Actions.
4. Get an Anthropic API key from `console.anthropic.com`.
5. (Optional) Get a Brave API key from `api.search.brave.com`.

**Operator instructions go in `README.md`:**
> 1. Open this repo on GitHub → click **Actions**
> 2. Pick **"Scrape Exhibition"** in the left sidebar
> 3. Click **"Run workflow"** on the right
> 4. Fill in the five fields (URL, name, year, industry, your email), click the green **Run workflow** button
> 5. Wait ~30–60 min. The CSV will arrive in your email when it's done.
> 6. (If the email gets lost: open the run on GitHub and download the CSV from the **Artifacts** section at the bottom.)

---

## 9. Quality assurance

**Per-row quality:**
- Email regex-validated (`local@domain.tld`)
- Domain has at least one dot, no whitespace
- Email domain matches company website domain (heuristic — bumps confidence to `high` even from LLM extraction)

**Per-run quality:**
- Log summary printed at end: `1000 companies seen, 870 with website, 712 with email (regex: 580, LLM: 132), 158 no email found`
- If <50% of companies got an email, flag the run as suspect (likely a parser broke) — surface in workflow summary

**Failure modes & fallbacks:**

| Failure | Fallback |
|---|---|
| Detail page 403 | Skip detail, Google-search company name for website |
| Company website JS-rendered, httpx returns empty | Retry with Playwright |
| Company website Cloudflare-protected | Skip, log, leave email empty |
| Email is image/JS-obfuscated | Skip in v1; consider OCR/JS exec in v2 |
| Run interrupted | Resume from cache on rerun |
| LLM rate-limited | Exponential backoff, max 3 retries |

---

## 10. Cost estimate

| Item | Per run (1k companies) | Per month (4 runs) |
|---|---|---|
| GitHub Actions minutes | ~60 min on `ubuntu-latest` (free tier: 2000 min/mo) | 240 min — within free tier |
| Anthropic Haiku | ~$1.50 | ~$6 |
| Playwright/proxies | $0 (no proxies) | $0 |
| **Total** | **~$1.50** | **~$6** |

Effectively free to operate.

---

## 11. Roadmap

**v0.1 — Listing only.** Playwright scrapes CPHI listing, prints company names. No CSV yet. Validates that the listing scraper works.

**v0.2 — Detail pages.** Adds website/country/booth extraction. Writes minimal CSV. Validates detail page access (the 403 issue).

**v0.3 — Email finder, regex only.** Adds the company-website crawl. Most leads should have emails by this stage.

**v0.4 — LLM fallback.** Wires in Haiku for hard cases.

**v0.5 — GitHub Actions wrapper.** Workflow, inputs, artifact upload. End-to-end runnable by the operator.

**v1.0 — Polish.** README for operator, error handling pass, run summary, tests.

**v2 (future):**
- Email notification on workflow completion (Gmail SMTP step)
- Notion-driven trigger (queue exhibitions in a Notion DB, cron polls it)
- Generalization to other exhibition platforms (LLM-driven generic extractor — feed listing HTML to Haiku, get JSON exhibitor list out)
- Master-CSV append + dedup by `(company_name, country)` across runs

---

## 12. Open questions to resolve during build

1. **Cache TTL.** Does a re-run within 24h reuse cached HTML? (Probably yes, with a `--no-cache` flag for forced refresh.)
2. **Search fallback reliability.** Start with DuckDuckGo HTML scrape (no key, free). If it rate-limits or breaks (Devansh has hit issues here before), swap to Brave Search API (free 2000/mo tier, needs `BRAVE_API_KEY`). Keep both implementations behind a single `find_website(company_name)` interface so the swap is one line.
3. **Run summary in email body.** Include lead-count stats ("1000 seen, 870 with website, 712 with email") in the email body so the operator sees quality at a glance without opening the CSV.
4. **What happens if the CPHI listing page changes its DOM structure?** v1 will break silently. Plan: a smoke test that asserts ≥50 cards extracted from a known-good URL; CI fails fast on regression.
5. **Failure email.** Should we send a separate "scrape failed" email on `if: failure()`? Probably yes — silent failures are the worst UX for a non-technical operator.
