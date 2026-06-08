# Exhibition Website Scraper

Pulls a lead list (company name + email + phone + address + country + booth)
from an exhibition site and emails it back to you as a CSV.

Built for an exhibition-stall business that needs to reach the companies
attending a given show. One scrape = one CSV = one email blast worth of leads.

## Supported sites

The scraper auto-detects which site you've given it from the URL prefix.

| Site | Example URL | Typical email rate |
|---|---|---|
| **CPHI** (pharma) | `https://exhibitors.cphi.com/cpww26/` | ~73% (named contacts) |
| **FI Global** (food ingredients) | `https://exhibitors.figlobal.com/live/figlobal/event46.jsp?site=47&type=company&eventid=629&map=false` | ~85% |
| **EuroTier** (agriculture) | `https://digital.eurotier.com/newfront/marketplace/exhibitors?pageNumber=1&limit=60` | ~100% |
| **Electronica** (electronics) | `https://exhibitors.electronica.de/exhibitor-portal/2026/` | ~20% (most contacts login-gated) |
| **Space Tech Expo Europe** | `https://www.spacetechexpo-europe.com/exhibitor-list/` | 0% (platform doesn't expose emails — website + address only) |

Coverage varies because each platform exposes different fields. A site that
hides contacts behind a login wall (like Electronica) yields lower email rates;
a site that publishes them publicly (like CPHI / EuroTier) yields high rates.

---

## For the operator (running a scrape)

You don't need to install anything. Everything runs in the cloud.

1. Open this repo on GitHub → click the **Actions** tab at the top
2. In the left sidebar, pick **"Scrape Exhibition"**
3. On the right, click the **"Run workflow"** dropdown
4. Fill in five fields:

   | Field | Example | What it does |
   |---|---|---|
   | `exhibition_url` | `https://exhibitors.cphi.com/cpww26/` | The exhibitor-list page |
   | `exhibition_name` | `CPHI Milan` | Used in the CSV and email subject |
   | `exhibition_year` | `2026` | Used in the CSV and email subject |
   | `industry` | `Pharma` | Tag for downstream analytics |
   | `recipient_email` | `you@example.com` | Where the CSV gets emailed |

5. Click the green **"Run workflow"** button.
6. Wait. A run of ~2,000 exhibitors takes about **2 hours**. You'll get an email
   with the CSV attached when it finishes (or a failure email if something
   broke).

If the email gets lost, the CSV is also kept as a workflow artifact for 30 days:
go into the run, scroll to **Artifacts** at the bottom, download `leads-csv`.

---

## What's in the CSV

One row per exhibitor. 16 columns:

- **Tagging columns** (constant per run, useful for merging multiple CSVs in Excel/pandas):
  `exhibition_name`, `exhibition_year`, `industry`, `exhibition_url`, `scraped_at`
- **Contact data**:
  `company_name`, `country`, `booth_number`, `company_email`, `company_phone`,
  `company_website` (derived from email domain), `address`
- **Provenance**:
  `company_profile_url` (back-link to the CPHI page), `email_source`
  (`jsonld` or `not_found`), `email_confidence`, `notes`

Empty `company_email` cells mean that CPHI didn't expose one for that company.
v0.3 (planned) will close that gap by scraping the company's own website.

---

## For the maintainer (one-time setup)

The workflow needs to send email via Gmail SMTP. Two GitHub secrets must be set
before any run will email a CSV.

### 1. Create a Gmail app password

1. Use any Gmail account (yours, or a dedicated one like `leads.scraper@gmail.com`)
2. The account must have **2-Step Verification enabled** (Google requires this for app passwords)
3. Go to: <https://myaccount.google.com/apppasswords>
4. Generate a new app password (label it "Exhibition Scraper" or similar)
5. Copy the 16-character password — you'll only see it once

### 2. Add the secrets to this repo

1. Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
2. Add **two** secrets:
   - Name: `GMAIL_USER` &nbsp; Value: the sending Gmail address (e.g. `leads.scraper@gmail.com`)
   - Name: `GMAIL_APP_PASSWORD` &nbsp; Value: the 16-char app password from step 1

### 3. Verify with a small test run

Trigger the workflow with `exhibition_url = https://exhibitors.cphi.com/cpww26/`
and your own email as `recipient_email`. Within ~2 hours you should get an email
with a ~500 KB CSV attached.

---

## For developers (running locally)

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows
# source .venv/bin/activate # macOS/Linux
pip install playwright
python -m playwright install chromium

python -m scraper.run \
  --url https://exhibitors.cphi.com/cpww26/ \
  --exhibition-name "CPHI Milan" \
  --exhibition-year 2026 \
  --industry Pharma \
  --limit-profiles 20      # cap for quick testing
```

CSVs land in `output/`. Per-profile HTML caches in `cache/profile/` —
re-runs reuse them unless you pass `--no-cache`.

See [SPEC.md](./SPEC.md) for the full design, tech stack, and roadmap.
