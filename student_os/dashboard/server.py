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
import time
from collections.abc import Callable
from contextlib import closing
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import db
from ..library import cards, notes
from ..moodle.storage import resolve_local
from . import queries

STATIC = Path(__file__).parent / "static"
MAX_BODY = 2_000_000


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
                 opener: Callable[[Path], None] = open_in_default_app, ollama: notes.Ollama | None = None) -> None:
        self.root = root
        self.ollama = ollama or notes.Ollama()
        self.batch: notes.BatchProgress | None = None
        self.last_request = time.monotonic()  # for stopping a background dashboard when idle
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
        self.app.last_request = time.monotonic()
        if not self._host_ok():
            return self._send_error(HTTPStatus.FORBIDDEN, "Wrong host")
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._index()
        if url.path == "/api/ping":  # lets the Dock app tell if the dashboard is already running
            return self._json({"app": "study-desk"})
        if not self._token_ok(url):
            return self._send_error(HTTPStatus.FORBIDDEN, "Missing or wrong token")
        parts = [p for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        with closing(self.app.connect()) as conn:
            if parts == ["api", "overview"]:
                return self._json(queries.overview(conn, self.app.today()))
            if parts == ["api", "ai"]:
                status = self.app.ollama.status()
                status["waiting"] = len(notes.files_needing_notes(conn))
                status["batch"] = self.app.batch.as_dict() if self.app.batch else None
                return self._json(status)
            if parts == ["api", "cards"]:
                module = query.get("module", [""])[0]
                today = self.app.today()
                return self._json({"cards": cards.due_cards(conn, today, module_id=int(module) if module.isdigit() else None),
                                   "totals": cards.card_totals(conn, today)})
            if len(parts) == 3 and parts[:2] == ["api", "claude-prompt"] and parts[2].isdigit():
                try:
                    return self._json({"prompt": notes.claude_prompt(conn, int(parts[2]))})
                except notes.NotesError as exc:
                    return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
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
        self.app.last_request = time.monotonic()
        if not self._host_ok():
            return self._send_error(HTTPStatus.FORBIDDEN, "Wrong host")
        url = urlparse(self.path)
        if not self._token_ok(url):
            return self._send_error(HTTPStatus.FORBIDDEN, "Missing or wrong token")
        parts = [p for p in url.path.split("/") if p]
        if parts == ["api", "notes", "all"]:
            if self.app.batch is None or not self.app.batch.running:
                self.app.batch = notes.run_in_background(self.app.connect, self.app.ollama)
            return self._json(self.app.batch.as_dict())
        if parts == ["api", "notes", "stop"]:
            if self.app.batch:
                self.app.batch.stop.set()
            return self._json({"stopping": True})
        if len(parts) == 4 and parts[:2] == ["api", "notes"] and parts[2].isdigit() and parts[3] in ("ollama", "claude"):
            return self._make_notes(int(parts[2]), parts[3])
        if len(parts) == 4 and parts[:2] == ["api", "cards"] and parts[2].isdigit() and parts[3] == "review":
            body = self._body()
            with closing(self.app.connect()) as conn:
                try:
                    return self._json(cards.record_review(conn, int(parts[2]), str(body.get("grade")), self.app.today()))
                except KeyError:
                    return self._send_error(HTTPStatus.NOT_FOUND, "No such card")
                except ValueError as exc:
                    return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        if len(parts) == 3 and parts[:2] == ["api", "open"] and parts[2].isdigit():
            with closing(self.app.connect()) as conn:
                path = self._local_file(conn, int(parts[2]))
            if path is None:
                return self._send_error(HTTPStatus.NOT_FOUND, "No local file")
            self.app.opener(path)
            return self._json({"opened": True})
        self._send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _make_notes(self, resource_id: int, route: str) -> None:
        body = self._body() if route == "claude" else {}
        with closing(self.app.connect()) as conn:
            try:
                if route == "ollama":
                    notes.make_notes_for(conn, resource_id, self.app.ollama)
                else:
                    notes.save_notes(conn, resource_id, notes.parse_reply(str(body.get("reply", ""))), self.app.today())
            except notes.NotesError as exc:
                return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return self._json(notes.get_notes(conn, resource_id))

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

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
