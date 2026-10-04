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
AUTO_LOGIN_WAIT_MS = 30_000
USERNAME_FIELD = "form#login input#username, input#username"
LOGIN_BUTTON = "form#login #loginbtn, #loginbtn, form#login button[type=submit]"
# Moodle's identity-provider buttons (standard markup), or links into its SSO plugins.
SSO_BUTTONS = ("a.login-identityprovider-btn, .potentialidp a, a[href*='/auth/oidc/'], "
               "a[href*='/auth/saml2/'], a[href*='/auth/shibboleth/']")


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


def sign_in(page: Any, *, timeout_s: int = SIGN_IN_TIMEOUT_S, auto_wait_ms: int = AUTO_LOGIN_WAIT_MS) -> None:
    """Open My Modules, signing in if needed.

    Moodle forgets its own session when the browser closes. On Moodle's login
    page we try, once each, what a person would press:

    1. the login form's "Log in" button, but only if the browser has already
       filled in a saved username (the browser's password manager fills the
       password; this code never reads or stores it);
    2. the university single-sign-on button, in case that sign-in is remembered.

    If neither finishes by itself, we wait for the user (e.g. for MFA).
    """
    page.goto(COURSES_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    if signed_in_url(page.url):
        return
    for attempt, how in ((submit_saved_login, "saved browser login"), (press_university_login, "university sign-in")):
        if on_login_page(page.url) and attempt(page) and _reached_moodle(page, auto_wait_ms):
            print(f"Signed in to Moodle automatically ({how}).", flush=True)
            return
    print("Sign in to Moodle in the browser window (including MFA). Waiting...", flush=True)
    page.wait_for_url(signed_in_url, timeout=timeout_s * 1000)


def on_login_page(url: str) -> bool:
    return allowed(url) and urlparse(url).path.startswith("/login/")


def submit_saved_login(page: Any) -> bool:
    """Press "Log in" on Moodle's form if the browser has filled in a saved username."""
    try:
        username = page.locator(USERNAME_FIELD).first
        if username.count() == 0:
            return False
        username.click(timeout=5_000)  # a real click lets the browser release its autofilled values
        if not username.input_value().strip():
            return False
        button = page.locator(LOGIN_BUTTON).first
        if button.count() == 0:
            return False
        button.click(timeout=5_000)
        return True
    except Exception:
        return False


def press_university_login(page: Any) -> bool:
    """On Moodle's own login page, click the single-sign-on button. Returns True if clicked."""
    if not on_login_page(page.url):
        return False
    try:
        button = page.locator(SSO_BUTTONS).first
        if button.count() == 0:
            return False
        button.click(timeout=5_000)
        return True
    except Exception:
        return False


def _reached_moodle(page: Any, timeout_ms: int) -> bool:
    try:
        page.wait_for_url(signed_in_url, timeout=timeout_ms)
        return True
    except Exception:
        return False


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
