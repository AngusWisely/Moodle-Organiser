"""``python3 scripts/dashboard.py``: open the study dashboard in your browser.

``--background`` is what the Study desk Dock app runs: if the dashboard is
already running it just opens the page; otherwise it starts the dashboard as a
quiet background process (no Terminal window), waits for it, then opens the
page. A background dashboard stops itself after a few idle hours.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from contextlib import closing
from pathlib import Path

from .. import db
from ..library.index import index_library
from .server import Dashboard

DEFAULT_PORT = 8765
BACKGROUND_IDLE_MINUTES = 180
START_TIMEOUT_S = 30


def main(root: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard.py", description="Open the study dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"local port (default {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    parser.add_argument("--reindex", action="store_true", help="re-read every file (after an upgrade)")
    parser.add_argument("--background", action="store_true",
                        help="open the page, starting the dashboard quietly in the background if needed")
    parser.add_argument("--idle-minutes", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not (root / "data" / "moodle.sqlite3").exists():
        print("No synced data yet. Run python3 scripts/sync_moodle.py first.")
        return 1
    if args.background:
        return open_in_background(root, args.port)
    return serve(root, port=args.port, open_browser=not args.no_browser, reindex=args.reindex,
                 idle_minutes=args.idle_minutes)


# --- serving ---------------------------------------------------------------------------

def serve(root: Path, *, port: int, open_browser: bool, reindex: bool = False, idle_minutes: int = 0) -> int:
    app = Dashboard(root)
    try:
        server = app.make_server(port)
    except OSError:
        server = app.make_server(0)  # port busy: use any free one
    actual = server.server_address[1]
    port_file(root).write_text(str(actual))
    url = f"http://127.0.0.1:{actual}/"
    threading.Thread(target=_update_library, args=(root, reindex), daemon=True).start()
    if idle_minutes:
        threading.Thread(target=_stop_when_idle, args=(app, server, idle_minutes * 60), daemon=True).start()
    print(f"Study desk is open at {url}  (press Ctrl+C here to close it)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nClosed.")
    finally:
        server.server_close()
        if port_file(root).exists() and port_file(root).read_text().strip() == str(actual):
            port_file(root).unlink(missing_ok=True)
    return 0


def _update_library(root: Path, reindex: bool) -> None:
    """Read new files for search in the background, so the page opens straight away."""
    with closing(db.connect(root / "data" / "moodle.sqlite3")) as conn:
        report = index_library(conn, root, rebuild=reindex)
    if report.indexed:
        print(f"Read {report.indexed} new file{'s' if report.indexed != 1 else ''} for search.", flush=True)


def _stop_when_idle(app: Dashboard, server, idle_seconds: float) -> None:
    while True:
        time.sleep(min(60.0, idle_seconds))
        busy = app.batch is not None and app.batch.running
        if not busy and time.monotonic() - app.last_request > idle_seconds:
            server.shutdown()
            return


# --- the Dock app's launcher -------------------------------------------------------------------

def port_file(root: Path) -> Path:
    return root / "data" / "dashboard.port"


def running_port(root: Path) -> int | None:
    """The port of a Study desk already running for this project, if any."""
    try:
        port = int(port_file(root).read_text().strip())
    except (OSError, ValueError):
        return None
    return port if ping(port) else None


def ping(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as response:
            return json.loads(response.read()).get("app") == "study-desk"
    except (OSError, ValueError):
        return False


def open_in_background(root: Path, port: int, *, opener=webbrowser.open, spawn=None) -> int:
    """Open the page, first starting a quiet background dashboard if none is running."""
    found = running_port(root)
    if found is None:
        port_file(root).unlink(missing_ok=True)
        (spawn or _spawn_background)(root, port)
        deadline = time.monotonic() + START_TIMEOUT_S
        while found is None and time.monotonic() < deadline:
            time.sleep(0.25)
            found = running_port(root)
        if found is None:
            print(f"The Study desk didn't start. Details are in {root / 'data' / 'dashboard.log'}")
            return 1
    opener(f"http://127.0.0.1:{found}/")
    return 0


def _spawn_background(root: Path, port: int) -> None:
    log = (root / "data" / "dashboard.log").open("a")
    subprocess.Popen(
        [sys.executable, str(root / "scripts" / "dashboard.py"), "--no-browser", "--port", str(port),
         "--idle-minutes", str(BACKGROUND_IDLE_MINUTES)],
        cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
