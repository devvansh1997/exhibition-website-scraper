"""Shared throttling + browser-identity helpers."""

from __future__ import annotations

import random
import time

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def jittered_sleep(base: float = 2.0, jitter: float = 1.0) -> None:
    """Sleep for `base + uniform(0, jitter)` seconds. Used between
    requests to the same site to avoid hammering."""
    time.sleep(base + random.uniform(0, jitter))
