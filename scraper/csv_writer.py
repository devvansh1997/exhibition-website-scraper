"""Write per-run CSVs with a consistent schema for downstream merging."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class RunMetadata:
    exhibition_name: str
    exhibition_year: int
    industry: str
    exhibition_url: str
    scraped_at: str  # ISO-ish, file-system safe


def _slugify(s: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in s.lower()).strip("_")
    return safe or "untitled"


def output_path(meta: RunMetadata, output_dir: Path) -> Path:
    name = "_".join(
        [
            _slugify(meta.industry),
            _slugify(meta.exhibition_name),
            str(meta.exhibition_year),
            meta.scraped_at,
        ]
    ) + ".csv"
    return output_dir / name


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
