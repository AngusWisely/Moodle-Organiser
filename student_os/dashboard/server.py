"""A small local web server for the dashboard. Standard library only.

Safety: it listens on 127.0.0.1 only, rejects requests whose Host isn't that
address (DNS-rebinding), and requires a random per-run token on every API
call and file, so other websites open in the browser can't use it. Files are
served only from paths the database knows, resolved inside the project.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import db
from ..moodle.storage import resolve_local
from . import queries

STATIC = Path(__file__).parent / "static"


def open_in_default_app(path: Path) -> None:
    """Open a file the way double-clicking it would."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":  # pragma: no cover
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:  # pragma: no cover
        subprocess.Popen(["xdg-open", str(path)])


class Dashboard:
    """Everything a request handler needs: where the data is and the session token."""

    def __init__(self, root: Path, *, token: str | None = None, today: Callable[[], date] = date.today,
                 opener: Callable[[Path], None] = open_in_default_app) -> None:
        self.root = root
        self.db_path = root / "data" / "moodle.sqlite3"
        self.token = token or secrets.token_urlsafe(24)
        self.today = today
        self.opener = opener

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def make_server(self, port: int = 0) -> ThreadingHTTPServer:
        dashboard = self

        class Handler(DashboardHandler):
            app = dashboard

        return ThreadingHTTPServer(("127.0.0.1", port), Handler)


class DashboardHandler(BaseHTTPRequestHandler):
    app: Dashboard
    server_version = "StudentOS"

    def log_message(self, format: str, *args) -> None:  # keep the terminal quiet
        pass

    # --- routing ---------------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send_error(HTTPStatus.FORBIDDEN, "Wrong host")
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._index()
        if not self._token_ok(url):
            return self._send_error(HTTPStatus.FORBIDDEN, "Missing or wrong token")
        parts = [p for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        with closing(self.app.connect()) as conn:
            if parts == ["api", "overview"]:
                return self._json(queries.overview(conn, self.app.today()))
            if parts == ["api", "search"]:
                return self._json(queries.search_results(conn, query.get("q", [""])[0]))
            if len(parts) == 3 and parts[:2] == ["api", "module"] and parts[2].isdigit():
                return self._json_or_404(queries.module_detail(conn, int(parts[2])))
            if len(parts) == 3 and parts[:2] == ["api", "resource"] and parts[2].isdigit():
                return self._json_or_404(queries.resource_detail(conn, int(parts[2]), self.app.today()))
            if len(parts) == 2 and parts[0] == "files" and parts[1].isdigit():
                return self._file(conn, int(parts[1]))
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._send_error(HTTPStatus.FORBIDDEN, "Wrong host")
        url = urlparse(self.path)
        if not self._token_ok(url):
            return self._send_error(HTTPStatus.FORBIDDEN, "Missing or wrong token")
        parts = [p for p in url.path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "open"] and parts[2].isdigit():
            with closing(self.app.connect()) as conn:
                path = self._local_file(conn, int(parts[2]))
            if path is None:
                return self._send_error(HTTPStatus.NOT_FOUND, "No local file")
            self.app.opener(path)
            return self._json({"opened": True})
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    # --- checks ------------------------------------------------------------------------

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _token_ok(self, url) -> bool:
        supplied = self.headers.get("X-Token") or parse_qs(url.query).get("t", [""])[0]
        return secrets.compare_digest(supplied, self.app.token)

    # --- responses ---------------------------------------------------------------------

    def _index(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", self.app.token)
        self._send(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8")

    def _json(self, data) -> None:
        self._send(HTTPStatus.OK, json.dumps(data).encode(), "application/json")

    def _json_or_404(self, data) -> None:
        if data is None:
            return self._send_error(HTTPStatus.NOT_FOUND, "Not found")
        self._json(data)

    def _local_file(self, conn: sqlite3.Connection, resource_id: int) -> Path | None:
        row = conn.execute("SELECT local_path FROM resources WHERE id = ?", (resource_id,)).fetchone()
        if row is None or not row["local_path"]:
            return None
        try:
            path = resolve_local(self.app.root, row["local_path"])
        except ValueError:
            return None
        return path if path.is_file() else None

    def _file(self, conn: sqlite3.Connection, resource_id: int) -> None:
        path = self._local_file(conn, resource_id)
        if path is None:
            return self._send_error(HTTPStatus.NOT_FOUND, "No local file")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, path.read_bytes(), content_type, filename=path.name)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, *, filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if filename:
            safe = filename.encode("ascii", "replace").decode().replace('"', "'")
            self.send_header("Content-Disposition", f'inline; filename="{safe}"')
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send(status, json.dumps({"error": message}).encode(), "application/json")
