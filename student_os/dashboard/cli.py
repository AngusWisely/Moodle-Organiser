"""``python3 scripts/dashboard.py``: open the study dashboard in your browser."""

from __future__ import annotations

import argparse
import webbrowser
from contextlib import closing
from pathlib import Path

from .. import db
from ..library.index import index_library
from .server import Dashboard

DEFAULT_PORT = 8765


def main(root: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard.py", description="Open the study dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"local port (default {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    parser.add_argument("--reindex", action="store_true", help="re-read every file (after an upgrade)")
    args = parser.parse_args(argv)

    if not (root / "data" / "moodle.sqlite3").exists():
        print("No synced data yet. Run python3 scripts/sync_moodle.py first.")
        return 1
    with closing(db.connect(root / "data" / "moodle.sqlite3")) as conn:
        print("Reading new files for search...", flush=True)
        report = index_library(conn, root, rebuild=args.reindex)
        if report.indexed:
            print(f"  {report.indexed} file{'s' if report.indexed != 1 else ''} read.")
        for problem in report.failed[:5]:
            print(f"  Couldn't read {problem}")

    app = Dashboard(root)
    try:
        server = app.make_server(args.port)
    except OSError:
        server = app.make_server(0)  # port busy (dashboard already open?): use any free one
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Study desk is open at {url}  (press Ctrl+C here to close it)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nClosed.")
    finally:
        server.server_close()
    return 0
