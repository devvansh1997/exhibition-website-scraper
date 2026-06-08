"""HTML-on-disk cache, namespaced by site.

Layout: {cache_dir}/{site_id}/{key}.html
Re-runs reuse cached HTML unless caller passes use_cache=False.
"""

from __future__ import annotations

from pathlib import Path


def cache_path(cache_dir: Path | None, site_id: str, key: str) -> Path | None:
    if cache_dir is None:
        return None
    return cache_dir / site_id / f"{key}.html"


def load_cached(cache_file: Path | None) -> str | None:
    if cache_file is None or not cache_file.exists():
        return None
    try:
        return cache_file.read_text(encoding="utf-8")
    except OSError:
        return None


def store_cached(cache_file: Path | None, html: str) -> None:
    if cache_file is None:
        return
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(html, encoding="utf-8")
    except OSError:
        pass  # cache is best-effort, never fatal
