"""HTTP layer against a fake Moodle: probe, downloads, conditional requests, login and redirect safety."""

from dataclasses import dataclass, field

import pytest

from student_os.moodle.fetch import (
    AuthExpired,
    Downloaded,
    ExternalRedirect,
    FetchError,
    NotAFile,
    PlaywrightTransport,
    Unmodified,
    fetch_file,
    looks_like_login,
    probe_resource,
)

M = "https://moodle.nottingham.ac.uk"
VIEW = f"{M}/mod/resource/view.php?id=4521"
FILE = f"{M}/pluginfile.php/77/mod_resource/content/4/notes.pdf"
LOGIN_HTML = '<form action="/login/index.php"><input type="hidden" name="logintoken" value="x"></form>'


@dataclass
class FakeResponse:
    status: int
    url: str
    headers: dict = field(default_factory=dict)
    content: bytes = b""

    def body(self) -> bytes:
        return self.content


class FakeMoodle:
    """Answers GETs from a table of url -> response, recording every request."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, *, headers, timeout_ms):
        self.requests.append((url, dict(headers)))
        make = self.routes.get(url)
        if make is None:
            raise AssertionError(f"Unexpected request: {url}")
        return make(url, headers) if callable(make) else make


def redirect(url: str, location: str) -> FakeResponse:
    return FakeResponse(303, url, {"location": location})


def pdf(url: str, content: bytes = b"%PDF-1.7", **headers) -> FakeResponse:
    return FakeResponse(200, url, {"content-type": "application/pdf", **headers}, content)


def html(url: str, text: str) -> FakeResponse:
    return FakeResponse(200, url, {"content-type": "text/html; charset=utf-8"}, text.encode())


PROBE = VIEW + "&redirect=1"


# --- probe -------------------------------------------------------------------

def test_probe_stops_at_pluginfile_redirect_without_downloading():
    moodle = FakeMoodle({PROBE: redirect(PROBE, FILE)})
    result = probe_resource(moodle, VIEW)
    assert result.file_url == FILE and result.direct is None and not result.is_video
    assert [u for u, _ in moodle.requests] == [PROBE]


def test_probe_handles_relative_redirects_and_detects_video():
    video = f"{M}/pluginfile.php/77/mod_resource/content/2/Week%201.MP4"
    moodle = FakeMoodle({PROBE: redirect(PROBE, "/pluginfile.php/77/mod_resource/content/2/Week%201.MP4")})
    result = probe_resource(moodle, VIEW)
    assert result.file_url == video and result.is_video


def test_probe_reads_file_link_from_embedded_resource_page():
    page = f'<div class="resourcecontent"><object data="{FILE}" type="application/pdf"></object></div>'
    moodle = FakeMoodle({PROBE: html(PROBE, page)})
    assert probe_resource(moodle, VIEW).file_url == FILE


def test_probe_page_without_file_link():
    moodle = FakeMoodle({PROBE: html(PROBE, "<p>Click here</p>")})
    assert probe_resource(moodle, VIEW).file_url is None


def test_probe_reuses_file_served_directly():
    moodle = FakeMoodle({PROBE: pdf(PROBE, b"abc", etag='"e1"')})
    result = probe_resource(moodle, VIEW)
    assert result.direct == Downloaded(b"abc", PROBE, "application/pdf", '"e1"', None)


@pytest.mark.parametrize("location", [f"{M}/login/index.php", "https://login.microsoftonline.com/x/oauth2"])
def test_probe_detects_expired_session(location):
    moodle = FakeMoodle({PROBE: redirect(PROBE, location)})
    with pytest.raises(AuthExpired):
        probe_resource(moodle, VIEW)


def test_probe_detects_login_page_served_in_place():
    moodle = FakeMoodle({PROBE: html(PROBE, LOGIN_HTML)})
    with pytest.raises(AuthExpired):
        probe_resource(moodle, VIEW)


def test_external_redirect_is_never_followed():
    moodle = FakeMoodle({PROBE: redirect(PROBE, "https://evil.example/steal")})
    with pytest.raises(ExternalRedirect):
        probe_resource(moodle, VIEW)
    assert len(moodle.requests) == 1


def test_redirect_loop_is_bounded():
    loop = f"{M}/mod/resource/loop.php"
    moodle = FakeMoodle({PROBE: redirect(PROBE, loop), loop: redirect(loop, loop)})
    with pytest.raises(FetchError, match="Too many redirects"):
        probe_resource(moodle, VIEW)


# --- downloads ---------------------------------------------------------------

def test_fetch_file_downloads_with_validators():
    moodle = FakeMoodle({FILE: pdf(FILE, b"data", etag='"e1"', **{"last-modified": "Sun, 04 Oct 2026 09:00:00 GMT"})})
    result = fetch_file(moodle, FILE)
    assert result == Downloaded(b"data", FILE, "application/pdf", '"e1"', "Sun, 04 Oct 2026 09:00:00 GMT")
    assert moodle.requests == [(FILE, {})]  # unconditional: no validators sent


def test_conditional_fetch_sends_validators_and_accepts_304():
    def answer(url, headers):
        assert headers == {"If-None-Match": '"e1"', "If-Modified-Since": "Sun, 04 Oct 2026 09:00:00 GMT"}
        return FakeResponse(304, url, {"etag": '"e1"'})

    moodle = FakeMoodle({FILE: answer})
    result = fetch_file(moodle, FILE, etag='"e1"', last_modified="Sun, 04 Oct 2026 09:00:00 GMT", conditional=True)
    assert result == Unmodified('"e1"', None)


def test_conditional_fetch_falls_back_to_body_when_server_ignores_validators():
    moodle = FakeMoodle({FILE: pdf(FILE, b"new")})
    assert isinstance(fetch_file(moodle, FILE, etag='"e1"', conditional=True), Downloaded)


def test_web_page_instead_of_file_is_not_saved():
    moodle = FakeMoodle({FILE: html(FILE, "<h1>Error</h1>")})
    with pytest.raises(NotAFile):
        fetch_file(moodle, FILE)


def test_login_page_instead_of_file_stops_the_run():
    moodle = FakeMoodle({FILE: html(FILE, LOGIN_HTML)})
    with pytest.raises(AuthExpired):
        fetch_file(moodle, FILE)


@pytest.mark.parametrize("status", [403, 404, 500])
def test_http_errors_raise(status):
    moodle = FakeMoodle({FILE: FakeResponse(status, FILE)})
    with pytest.raises(FetchError):
        fetch_file(moodle, FILE)


def test_fetch_follows_moodle_redirects_only():
    other = f"{M}/pluginfile.php/77/mod_resource/content/5/notes.pdf"
    moodle = FakeMoodle({FILE: redirect(FILE, other), other: pdf(other)})
    assert fetch_file(moodle, FILE).url == other


# --- transport and helpers ------------------------------------------------------

def test_playwright_transport_refuses_other_hosts_and_never_auto_redirects():
    calls = []

    class Request:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(200, url)

    class Context:
        request = Request()

    transport = PlaywrightTransport(Context(), min_interval=0)
    with pytest.raises(ValueError):
        transport.get("https://evil.example/", headers={}, timeout_ms=1)
    transport.get(FILE, headers={"If-None-Match": "x"}, timeout_ms=5)
    assert calls == [(FILE, {"headers": {"If-None-Match": "x"}, "max_redirects": 0, "timeout": 5})]


def test_looks_like_login():
    assert looks_like_login(f"{M}/login/index.php")
    assert looks_like_login(f"{M}/my/", LOGIN_HTML)
    assert not looks_like_login(f"{M}/my/", "<h1>My modules</h1>")
    assert not looks_like_login("https://evil.example/login/x")
