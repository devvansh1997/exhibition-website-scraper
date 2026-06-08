"""Extract schema.org JSON-LD blocks from HTML.

The CPHI scraper relies on `<script type="application/ld+json">` Organization
blocks to get named-contact emails for free. Other sites may have them too;
this module is the shared parser.
"""

from __future__ import annotations

import json
import re

JSONLD_PATTERN = re.compile(
    r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
    re.DOTALL,
)


def extract_organization(html: str) -> dict | None:
    """Find the first schema.org `Organization` block in the HTML. Walks
    `@graph` arrays. Returns the dict, or None if none found."""
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


def format_address(addr: dict | None) -> tuple[str, str]:
    """Flatten a schema.org PostalAddress dict into "(joined string, country_code)"."""
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
