"""Write per-run CSVs with a consistent schema for downstream merging."""

from __future__ import annotations

import csv
from pathlib import Path

from scraper.core.types import Lead, RunMetadata

CSV_COLUMNS = [
    "exhibition_name",
    "exhibition_year",
    "industry",
    "exhibition_url",
    "scraped_at",
    "company_name",
    "country",
    "booth_number",
    "company_email",
    "company_phone",
    "company_website",
    "address",
    "company_profile_url",
    "email_source",
    "email_confidence",
    "notes",
]


def _slugify(s: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in s.lower()).strip("_")
    return safe or "untitled"


def output_path(meta: RunMetadata, output_dir: Path) -> Path:
    name = (
        "_".join(
            [
                _slugify(meta.industry),
                _slugify(meta.exhibition_name),
                str(meta.exhibition_year),
                meta.scraped_at,
            ]
        )
        + ".csv"
    )
    return output_dir / name


def lead_to_row(lead: Lead, meta: RunMetadata) -> dict[str, str]:
    return {
        "exhibition_name": meta.exhibition_name,
        "exhibition_year": meta.exhibition_year,
        "industry": meta.industry,
        "exhibition_url": meta.exhibition_url,
        "scraped_at": meta.scraped_at,
        "company_name": lead.company_name,
        "country": lead.country,
        "booth_number": lead.booth_number,
        "company_email": lead.company_email,
        "company_phone": lead.company_phone,
        "company_website": lead.company_website,
        "address": lead.address,
        "company_profile_url": lead.company_profile_url,
        "email_source": lead.email_source,
        "email_confidence": lead.email_confidence,
        "notes": lead.notes,
    }


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
