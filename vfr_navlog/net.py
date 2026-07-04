"""The single HTTP entry point.

One place that knows about the User-Agent header and the error policy
(None on failure). Callers are migrated onto this in Phase 2.
"""
from __future__ import annotations

import urllib.error
import urllib.request

from .config import VATSIM_UA


def fetch(url: str, timeout: float = 6.0, ua: str = VATSIM_UA) -> str | None:
    """GET *url* and return the decoded body, or None on any network failure."""
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError):
        return None
