"""A fake Moodle for tests: answers GETs from a route table and records every request."""

from __future__ import annotations

from dataclasses import dataclass, field

M = "https://moodle.nottingham.ac.uk"
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
    """``routes`` maps url -> FakeResponse or a callable(url, headers). Edit it between runs."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, *, headers, timeout_ms):
        self.requests.append((url, dict(headers)))
        make = self.routes.get(url)
        if make is None:
            raise AssertionError(f"Unexpected request: {url}")
        return make(url, headers) if callable(make) else make

    def urls(self) -> list[str]:
        return [url for url, _ in self.requests]


def redirect(url: str, location: str) -> FakeResponse:
    return FakeResponse(303, url, {"location": location})


def pdf(url: str, content: bytes = b"%PDF-1.7", **headers) -> FakeResponse:
    return FakeResponse(200, url, {"content-type": "application/pdf", **headers}, content)


def html(url: str, text: str) -> FakeResponse:
    return FakeResponse(200, url, {"content-type": "text/html; charset=utf-8"}, text.encode())
