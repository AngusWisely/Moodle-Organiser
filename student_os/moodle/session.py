"""The signed-in browser: persistent profile, sign-in detection and course-page scanning.

Authentication stays here, separate from sync logic. No credentials are ever
stored: you sign in (with MFA) in the browser window when Moodle asks, and the
persistent profile keeps the session for later runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import Item
from .scraper import activities, course_links, section_links
from .urls import COURSES_URL, allowed

SIGN_IN_TIMEOUT_S = 600
PAGE_TIMEOUT_MS = 60_000


@dataclass(frozen=True)
class CourseScan:
    """Everything found on a course. ``complete`` is False if any part failed to load."""

    items: list[Item]
    complete: bool
    problems: list[str]


@contextmanager
def moodle_browser(profile: Path) -> Iterator[tuple[Any, Any]]:
    """Open Chromium with the persistent profile. Yields ``(context, page)``."""
    from playwright.sync_api import sync_playwright  # imported here so tests never need a browser

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), headless=False,
                                                                accept_downloads=True)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield context, page
        finally:
            context.close()


def signed_in_url(url: str) -> bool:
    """True once the browser is on a signed-in Moodle page (dashboard or My Modules)."""
    return allowed(url) and urlparse(url).path.startswith("/my/")


def sign_in(page: Any, *, timeout_s: int = SIGN_IN_TIMEOUT_S) -> None:
    """Open My Modules; if Moodle asks for sign-in, wait for the user to finish it."""
    page.goto(COURSES_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    if signed_in_url(page.url):
        return
    print("Sign in to Moodle in the browser window (including MFA). Waiting...", flush=True)
    page.wait_for_url(signed_in_url, timeout=timeout_s * 1000)


def list_courses(page: Any) -> list[tuple[str, str]]:
    """Return ``(name, url)`` for each course on My Modules."""
    page.goto(COURSES_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(2000)  # course cards are rendered by JavaScript
    return course_links(page.content(), page.url)


def scan_course(page: Any, module: str, url: str) -> CourseScan:
    """Collect every activity link on a course, including separate section pages."""
    problems: list[str] = []
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(1000)
    for button in page.get_by_text("Expand all", exact=True).all()[:2]:
        try:
            button.click(timeout=1500)
        except Exception:
            pass
    html = page.content()
    if not signed_in_course(page.url):
        return CourseScan([], False, ["Course page did not load (signed out?)"])
    found = activities(html, module, page.url)
    for section_url in section_links(html, page.url):
        try:
            page.goto(section_url, wait_until="domcontentloaded", timeout=45_000)
            found.extend(activities(page.content(), module, page.url))
        except Exception as exc:
            problems.append(f"Section page failed: {type(exc).__name__}")
    unique = list({item.url: item for item in found}.values())
    return CourseScan(unique, complete=not problems, problems=problems)


def signed_in_course(url: str) -> bool:
    return allowed(url) and urlparse(url).path.startswith("/course/")
