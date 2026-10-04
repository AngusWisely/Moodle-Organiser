"""Moodle base URLs and the host allow-list.

Every URL the tool requests or follows must pass :func:`allowed`, so session
cookies are only ever sent to Nottingham Moodle.
"""

from __future__ import annotations

from urllib.parse import urlparse

BASE = "https://moodle.nottingham.ac.uk"
COURSES_URL = f"{BASE}/my/courses.php"
HOST = "moodle.nottingham.ac.uk"


def allowed(url: str) -> bool:
    """Return True only for HTTPS URLs on the exact Nottingham Moodle host."""
    parts = urlparse(url)
    return parts.scheme == "https" and parts.hostname == HOST
