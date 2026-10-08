"""``python scripts/sync_moodle.py``: one command to bring materials/ up to date with Moodle."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import db
from .detector import Outcome
from .fetch import AuthExpired, PlaywrightTransport
from .storage import clean_incoming
from .sync import ModuleReport, SyncOptions, Syncer


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sync_moodle.py", description="Sync Nottingham Moodle teaching materials")
    parser.add_argument("--select", action="store_true", help="choose which modules to sync (remembered)")
    parser.add_argument("--videos", action="store_true", help="download videos instead of linking them")
    parser.add_argument("--refresh", action="store_true",
                        help="download and hash every file to catch silent changes (slow)")
    parser.add_argument("--dry-run", action="store_true",
                        help="check Moodle and report, without downloading or changing anything")
    parser.add_argument("--recent", type=float, metavar="DAYS", nargs="?", const=1.0,
                        help="list changes from the last DAYS days (default 1) and exit; no browser")
    return parser.parse_args(argv)


def main(root: Path, argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    materials = root / "materials"
    conn = db.connect(root / "data" / "moodle.sqlite3")
    try:
        if args.recent is not None:
            print_recent(conn, args.recent)
            return 0
        first_run_import(conn, root, materials)
        clean_incoming(materials)
        work = memory_copy(conn) if args.dry_run else conn
        options = SyncOptions(refresh=args.refresh, videos=args.videos, dry_run=args.dry_run)
        return run(work, root, materials, options, select=args.select)
    finally:
        conn.close()


def run(conn: sqlite3.Connection, root: Path, materials: Path, options: SyncOptions, *, select: bool) -> int:
    from .session import list_courses, moodle_browser, scan_course, sign_in

    print("Moodle Sync" + (" (dry run: nothing will be downloaded or changed)" if options.dry_run else ""))
    with moodle_browser(root / ".browser-profile") as (context, page):
        try:
            sign_in(page)
            courses = list_courses(page)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if not options.dry_run:
                with conn:
                    failed_run = db.start_run(conn, mode(options), db.utc_now())
                    db.finish_run(conn, failed_run, "auth_failed" if "/login/" in page.url else "aborted", db.utc_now())
            print(f"Could not reach My Modules: {type(exc).__name__}. Sign in and run the sync again.")
            return 2
        if not courses:
            if not options.dry_run:
                with conn:
                    failed_run = db.start_run(conn, mode(options), db.utc_now())
                    db.finish_run(conn, failed_run, "auth_failed", db.utc_now())
            print("No modules found on My Modules. Check that it shows all courses.")
            return 1
        on_moodle = record_courses(conn, courses)
        modules = choose_modules(conn, on_moodle, force=select)
        with conn:
            run_id = db.start_run(conn, mode(options), db.utc_now())
        syncer = Syncer(conn, PlaywrightTransport(context), root=root, materials=materials, run_id=run_id,
                        options=options)
        reports: list[ModuleReport] = []
        outcome = "completed"
        try:
            for module in modules:
                print(f"\n{module.name}", flush=True)
                scan = scan_course(page, module.name, module.url or "")
                report = syncer.sync_module(module, scan)
                reports.append(report)
                print(format_module(report), flush=True)
                if not options.dry_run:
                    db.export_index_csv(conn, materials / "index.csv")
        except AuthExpired as exc:
            outcome = "auth_failed"
            print(f"\nStopped: {exc}. Run again and sign in when asked; nothing already saved is lost.")
        except KeyboardInterrupt:
            outcome = "aborted"
            print("\nStopped. Everything synced so far is saved.")
        finally:
            if outcome == "completed" and any(r.problems or r.counts[Outcome.FAILED] for r in reports):
                outcome = "completed_with_errors"
            with conn:
                db.finish_run(conn, run_id, outcome, db.utc_now(), bytes_received=syncer.bytes_received,
                              selected_count=len(modules), scanned_count=len(reports))
        print("\n" + format_totals(reports, dry_run=options.dry_run))
        others = len(on_moodle) - len(modules)
        if others > 0:
            print(f"({others} other module{'s' if others != 1 else ''} on Moodle not synced; use --select to change)")
        if outcome not in {"completed", "completed_with_errors"}:
            return 2
        return 1 if any(r.problems for r in reports) else 0


# --- modules -----------------------------------------------------------------------

def record_courses(conn: sqlite3.Connection, courses: list[tuple[str, str]]) -> list[db.Module]:
    """Upsert every course on My Modules; returns them in Moodle's order."""
    now = db.utc_now()
    with conn:
        return [db.upsert_module(conn, moodle_id=course_id(url), name=name, url=url, at=now) for name, url in courses]


def course_id(url: str) -> int | None:
    ids = parse_qs(urlparse(url).query).get("id", [])
    return int(ids[0]) if ids and ids[0].isdigit() else None


def choose_modules(conn: sqlite3.Connection, on_moodle: list[db.Module], *, force: bool) -> list[db.Module]:
    """Use the remembered selection, asking only on first run or with --select."""
    selected_ids = {m.id for m in db.selected_modules(conn)}
    current = [m for m in on_moodle if m.id in selected_ids]
    if current and not force:
        return current
    print("\nWhich modules should be synced?")
    for n, module in enumerate(on_moodle, 1):
        mark = "*" if module.id in selected_ids else " "
        print(f"{n:2}. [{mark}] {module.name}")
    chosen = ask_numbers(len(on_moodle))
    picked = [on_moodle[n - 1] for n in chosen]
    with conn:
        db.select_modules(conn, [m.id for m in picked])
    return picked


def ask_numbers(count: int) -> list[int]:
    while True:
        answer = input("\nModule numbers (e.g. 1,2,4) or all: ").strip().lower()
        if answer == "all":
            return list(range(1, count + 1))
        try:
            nums = sorted({int(part) for part in answer.split(",") if part.strip()})
        except ValueError:
            nums = []
        if nums and all(1 <= n <= count for n in nums):
            return nums
        print("Please enter numbers from the list.")


# --- reporting ---------------------------------------------------------------------

def format_module(report: ModuleReport) -> str:
    c = report.counts
    lines = [f"✓ {report.checked} checked"]
    for symbol, count, label in (("+", c[Outcome.NEW], "new"), ("~", c[Outcome.UPDATED], "updated"),
                                 ("↺", c[Outcome.RESTORED], "restored"), ("-", report.removed, "removed"),
                                 ("?", report.to_fetch, "to download or check"),
                                 ("!", c[Outcome.FAILED], "failed")):
        if count:
            lines.append(f"{symbol} {count} {label}")
    lines += [f"  ! {problem}" for problem in report.problems[:10]]
    if len(report.problems) > 10:
        lines.append(f"  ! ...and {len(report.problems) - 10} more")
    return "\n".join(lines)


def format_totals(reports: list[ModuleReport], *, dry_run: bool = False) -> str:
    total = {o: sum(r.counts[o] for r in reports) for o in Outcome}
    lines = [f"New: {total[Outcome.NEW]}", f"Updated: {total[Outcome.UPDATED]}",
             f"Unchanged: {total[Outcome.UNCHANGED]}", f"Linked: {total[Outcome.LINKED]}"]
    if total[Outcome.RESTORED]:
        lines.append(f"Restored: {total[Outcome.RESTORED]}")
    removed = sum(r.removed for r in reports)
    if removed:
        lines.append(f"Removed: {removed}")
    if dry_run:
        lines.append(f"Would download or check: {sum(r.to_fetch for r in reports)}")
    lines.append(f"Errors: {sum(len(r.problems) for r in reports)}")
    return "\n".join(lines)


def print_recent(conn: sqlite3.Connection, days: float) -> None:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(format_recent(db.events_since(conn, since), days))


def format_recent(events: list, days: float, tz: tzinfo | None = None) -> str:
    """Changes grouped by module, oldest first, in local time (or ``tz``)."""
    if not events:
        return f"No changes in the last {days:g} day(s)."
    lines = [f"Changes in the last {days:g} day(s)"]
    module = None
    for e in events:
        if e["module"] != module:
            module = e["module"]
            lines += ["", module]
        when = datetime.strptime(e["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        where = " / ".join(part for part in (e["section"], e["title"]) if part)
        lines.append(f"  {when.astimezone(tz):%d %b %H:%M}  {e['kind']:<10} {where}")
    return "\n".join(lines)


# --- setup -------------------------------------------------------------------------

def first_run_import(conn: sqlite3.Connection, root: Path, materials: Path) -> None:
    """Import organiser.py's index.csv once, so existing downloads count as the baseline."""
    has_resources = conn.execute("SELECT 1 FROM resources LIMIT 1").fetchone()
    index = materials / "index.csv"
    if has_resources or not index.is_file():
        return
    report = db.import_legacy_index(conn, root, index, db.utc_now())
    print(f"Imported {report.imported} items from index.csv ({report.hashed} existing files hashed).")
    if report.missing_files:
        print(f"  {len(report.missing_files)} indexed files are missing locally and will be downloaded again.")
    if report.unindexed_files:
        print(f"  {len(report.unindexed_files)} files in materials/ aren't in the index (left untouched).")


def memory_copy(conn: sqlite3.Connection) -> sqlite3.Connection:
    """An in-memory copy for dry runs: the real database is never written."""
    copy = sqlite3.connect(":memory:")
    conn.backup(copy)
    copy.row_factory = sqlite3.Row
    copy.execute("PRAGMA foreign_keys = ON")
    return copy


def mode(options: SyncOptions) -> str:
    return "dry_run" if options.dry_run else "refresh" if options.refresh else "normal"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(Path.cwd()))
