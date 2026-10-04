"""HTTP access to Moodle: the cheap resource probe, file downloads and login detection.

Everything goes through a :class:`Transport` that never follows redirects by
itself. This module follows them one hop at a time, re-checking every hop
against the Nottingham Moodle allow-list, so session cookies are never sent
elsewhere. Tests use a fake transport; the real one wraps Playwright's
request context, which shares the browser's signed-in cookies.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .storage import VIDEO_EXT
from .urls import allowed

REDIRECTS = {301, 302, 303, 307, 308}
MAX_HOPS = 8
PROBE_TIMEOUT_MS = 30_000
FILE_TIMEOUT_MS = 300_000


# --- errors -------------------------------------------------------------------

class FetchError(Exception):
    """A request did not produce what was expected. The resource's state is left alone."""


class AuthExpired(FetchError):
    """Moodle sent us to a login page: the session has ended. The run should stop."""


class ExternalRedirect(FetchError):
    """Moodle redirected away from Nottingham Moodle; the target was not requested."""

    def __init__(self, location: str) -> None:
        super().__init__(f"Redirects outside Moodle to {urlparse(location).hostname}")
        self.location = location


class NotAFile(FetchError):
    """A web page came back where a file was expected."""


# --- transport ----------------------------------------------------------------

class Response(Protocol):
    status: int
    url: str
    headers: Mapping[str, str]          # lower-case names

    def body(self) -> bytes: ...


class Transport(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], timeout_ms: int) -> Response:
        """One GET without following redirects."""


class PlaywrightTransport:
    """Transport over a Playwright browser context's request API (shares its cookies)."""

    def __init__(self, context: Any, *, min_interval: float = 0.2) -> None:
        self._request = context.request
        self._min_interval = min_interval
        self._last = 0.0

    def get(self, url: str, *, headers: Mapping[str, str], timeout_ms: int) -> Response:
        if not allowed(url):
            raise ValueError(f"Refusing to request non-Moodle URL: {url}")
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)  # be gentle with Moodle: requests are sequential and spaced
        try:
            return self._request.get(url, headers=dict(headers), max_redirects=0, timeout=timeout_ms)
        finally:
            self._last = time.monotonic()


# --- results ------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeResult:
    """Where a ``/mod/resource/view.php`` link really points."""

    file_url: str | None                 # pluginfile URL, including Moodle's revision
    direct: Downloaded | None = None     # Moodle served the file straight away; reuse it

    @property
    def is_video(self) -> bool:
        return self.file_url is not None and is_video_url(self.file_url)


@dataclass(frozen=True)
class Downloaded:
    """A complete file body and the headers worth remembering."""

    body: bytes
    url: str
    content_type: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class Unmodified:
    """304: the server confirms our copy is current."""

    etag: str | None
    last_modified: str | None


# --- public operations ----------------------------------------------------------

def probe_resource(transport: Transport, view_url: str, *, timeout_ms: int = PROBE_TIMEOUT_MS) -> ProbeResult:
    """Find the file behind a resource link without downloading it.

    Asks ``view.php?...&redirect=1`` and stops at the first redirect to a
    pluginfile URL. If Moodle shows a page instead (an embedded resource), the
    first pluginfile link on that page is used.
    """
    url = _with_param(view_url, "redirect", "1")
    for _ in range(MAX_HOPS):
        response = transport.get(url, headers={}, timeout_ms=timeout_ms)
        if response.status in REDIRECTS:
            url = _next_hop(response)
            if "/pluginfile.php/" in urlparse(url).path:
                return ProbeResult(url)
            continue
        _raise_for_status(response)
        content_type = _content_type(response)
        if content_type.startswith("text/html"):
            html = response.body().decode("utf-8", "replace")
            _raise_if_login(response.url, html)
            return ProbeResult(_first_file_link(html, response.url))
        return ProbeResult(response.url, direct=_downloaded(response))
    raise FetchError("Too many redirects")


def fetch_file(transport: Transport, url: str, *, etag: str | None = None, last_modified: str | None = None,
               conditional: bool = False, timeout_ms: int = FILE_TIMEOUT_MS) -> Downloaded | Unmodified:
    """Download a Moodle file, or ask whether it changed when ``conditional`` is set.

    Raises :class:`AuthExpired` for a login page and :class:`NotAFile` for any
    other web page, so neither is ever saved or mistaken for content.
    """
    headers: dict[str, str] = {}
    if conditional:
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
    for _ in range(MAX_HOPS):
        response = transport.get(url, headers=headers, timeout_ms=timeout_ms)
        if response.status in REDIRECTS:
            url = _next_hop(response)
            continue
        if response.status == 304 and conditional:
            return Unmodified(_header(response, "etag"), _header(response, "last-modified"))
        _raise_for_status(response)
        if _content_type(response).startswith("text/html"):
            _raise_if_login(response.url, response.body().decode("utf-8", "replace"))
            raise NotAFile("Moodle returned a web page, not a file")
        return _downloaded(response)
    raise FetchError("Too many redirects")


def fetch_page(transport: Transport, url: str, *, timeout_ms: int = PROBE_TIMEOUT_MS) -> tuple[str, str]:
    """Fetch a Moodle web page (e.g. a folder). Returns ``(final_url, html)``."""
    for _ in range(MAX_HOPS):
        response = transport.get(url, headers={}, timeout_ms=timeout_ms)
        if response.status in REDIRECTS:
            url = _next_hop(response)
            continue
        _raise_for_status(response)
        if not _content_type(response).startswith("text/html"):
            raise FetchError("Expected a web page")
        html = response.body().decode("utf-8", "replace")
        _raise_if_login(response.url, html)
        return response.url, html
    raise FetchError("Too many redirects")


def is_video_url(url: str) -> bool:
    return Path(unquote(urlparse(url).path)).suffix.lower() in VIDEO_EXT


def looks_like_login(url: str, html: str = "") -> bool:
    """Moodle's login page, or the university sign-in it hands off to."""
    parts = urlparse(url)
    if allowed(url) and parts.path.startswith("/login/"):
        return True
    return 'name="logintoken"' in html


# --- helpers ------------------------------------------------------------------

def _next_hop(response: Response) -> str:
    location = _header(response, "location")
    if not location:
        raise FetchError("Redirect had no location")
    target = urljoin(response.url, location)
    if looks_like_login(target):
        raise AuthExpired("Moodle session expired: redirected to sign-in")
    if not allowed(target):
        host = urlparse(target).hostname or ""
        if "microsoftonline" in host or host.startswith("login.") or "sso" in host:
            raise AuthExpired(f"Moodle session expired: redirected to {host}")
        raise ExternalRedirect(target)
    return target


def _raise_for_status(response: Response) -> None:
    if response.status in (401, 403):
        raise FetchError(f"Access denied (HTTP {response.status})")
    if not 200 <= response.status < 300:
        raise FetchError(f"HTTP {response.status}")
    if not allowed(response.url):
        raise ExternalRedirect(response.url)


def _raise_if_login(url: str, html: str) -> None:
    if looks_like_login(url, html):
        raise AuthExpired("Moodle session expired: sign-in page returned")


def _first_file_link(html: str, base: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for tag, attr in (("a", "href"), ("object", "data"), ("iframe", "src"), ("embed", "src"),
                      ("source", "src"), ("img", "src")):
        for node in soup.find_all(tag):
            value = node.get(attr)
            if value and "/pluginfile.php/" in value:
                url = urljoin(base, value).split("#", 1)[0]
                if allowed(url):
                    return url
    return None


def _downloaded(response: Response) -> Downloaded:
    return Downloaded(response.body(), response.url, _content_type(response),
                      _header(response, "etag"), _header(response, "last-modified"))


def _content_type(response: Response) -> str:
    return (_header(response, "content-type") or "").split(";", 1)[0].strip().lower()


def _header(response: Response, name: str) -> str | None:
    return response.headers.get(name) or response.headers.get(name.title())


def _with_param(url: str, key: str, value: str) -> str:
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key] + [(key, value)]
    return urlunparse(parts._replace(query=urlencode(query)))
