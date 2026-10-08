"""SQLite state for Student OS. Milestone 1 stores Moodle modules, resources and sync history.

The database is the source of truth; ``materials/index.csv`` is an export of it
for browsing in a spreadsheet. Timestamps are UTC ISO 8601 strings.

Functions here do not commit: callers group work with ``with conn:`` so a
resource's file move and its database update succeed or fail together.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .moodle.storage import INCOMING, VERSIONS, resolve_local, sha256_file
from .moodle.urls import cmid, resource_key

SCHEMA_VERSION = 2

# Resource.status values
PENDING = "pending"          # seen, no successful download yet (retried every run)
DOWNLOADED = "downloaded"
LINKED = "linked"            # page, assignment, external link, folder with no files...
VIDEO_LINK = "video_link"    # video recorded as a link (no --videos)
REMOVED = "removed"          # no longer on Moodle; local files kept

_SCHEMA = """
CREATE TABLE modules (
    id            INTEGER PRIMARY KEY,
    moodle_id     INTEGER UNIQUE,          -- course id; NULL for modules imported from index.csv until first sync
    name          TEXT NOT NULL,
    url           TEXT,
    selected      INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE resources (
    id                INTEGER PRIMARY KEY,
    key               TEXT NOT NULL UNIQUE, -- see moodle.urls.resource_key
    module_id         INTEGER NOT NULL REFERENCES modules(id),
    section           TEXT,
    title             TEXT NOT NULL,
    resource_type     TEXT NOT NULL,
    cmid              INTEGER,
    source_url        TEXT NOT NULL,
    file_url          TEXT,                 -- last resolved pluginfile URL, including revision
    etag              TEXT,
    last_modified     TEXT,
    local_path        TEXT,                 -- relative to the project root
    file_size         INTEGER,
    content_hash      TEXT,                 -- SHA-256 of the current local file
    legacy_url_digest TEXT,                 -- from organiser.py file names
    status            TEXT NOT NULL,
    last_error        TEXT,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    downloaded_at     TEXT,
    updated_at        TEXT                  -- last genuine content change
);
CREATE INDEX resources_module ON resources(module_id);

CREATE TABLE sync_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,              -- normal | refresh | dry_run | import
    outcome     TEXT NOT NULL               -- running | completed | aborted | auth_failed
);

CREATE TABLE sync_events (                  -- changes only; UNCHANGED is implied by last_seen_at
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES sync_runs(id),
    resource_id   INTEGER NOT NULL REFERENCES resources(id),
    kind          TEXT NOT NULL,            -- new | updated | restored | failed | removed | reappeared
    old_hash      TEXT,
    new_hash      TEXT,
    archived_path TEXT,
    message       TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX sync_events_created ON sync_events(created_at);
"""


# --- connection ----------------------------------------------------------------

def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the database and bring its schema up to date."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply schema changes using ``PRAGMA user_version``."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"Database schema {version} is newer than this code ({SCHEMA_VERSION})")
    if version < 1:
        with conn:
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA user_version = 1")
    if version < 2:
        with conn:
            conn.execute("ALTER TABLE sync_runs ADD COLUMN bytes_received INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE sync_runs ADD COLUMN selected_count INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE sync_runs ADD COLUMN scanned_count INTEGER NOT NULL DEFAULT 0")
            conn.execute("PRAGMA user_version = 2")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- records ----------------------------------------------------------------------

@dataclass(frozen=True)
class Module:
    id: int
    moodle_id: int | None
    name: str
    url: str | None
    selected: bool
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class Resource:
    id: int
    key: str
    module_id: int
    section: str | None
    title: str
    resource_type: str
    cmid: int | None
    source_url: str
    file_url: str | None
    etag: str | None
    last_modified: str | None
    local_path: str | None
    file_size: int | None
    content_hash: str | None
    legacy_url_digest: str | None
    status: str
    last_error: str | None
    first_seen_at: str
    last_seen_at: str
    downloaded_at: str | None
    updated_at: str | None


def _module(row: sqlite3.Row) -> Module:
    return Module(**{**dict(row), "selected": bool(row["selected"])})


def _resource(row: sqlite3.Row) -> Resource:
    return Resource(**dict(row))


# --- modules ----------------------------------------------------------------------

def upsert_module(conn: sqlite3.Connection, *, moodle_id: int | None, name: str, url: str | None,
                  at: str) -> Module:
    """Record a module seen on My Modules.

    Matches by Moodle course id; failing that, adopts a same-named module
    imported from index.csv (which has no course id yet).
    """
    row = None
    if moodle_id is not None:
        row = conn.execute("SELECT * FROM modules WHERE moodle_id = ?", (moodle_id,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM modules WHERE name = ? AND moodle_id IS NULL ORDER BY id LIMIT 1", (name,)
        ).fetchone()
    if row is None:
        module_id = conn.execute(
            "INSERT INTO modules (moodle_id, name, url, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (moodle_id, name, url, at, at),
        ).lastrowid
    else:
        module_id = row["id"]
        conn.execute(
            "UPDATE modules SET moodle_id = COALESCE(?, moodle_id), name = ?, url = COALESCE(?, url),"
            " last_seen_at = ? WHERE id = ?",
            (moodle_id, name, url, at, module_id),
        )
    return _module(conn.execute("SELECT * FROM modules WHERE id = ?", (module_id,)).fetchone())


def select_modules(conn: sqlite3.Connection, module_ids: Iterable[int]) -> None:
    """Make exactly these modules the ones synced by default."""
    ids = list(module_ids)
    conn.execute("UPDATE modules SET selected = 0")
    conn.executemany("UPDATE modules SET selected = 1 WHERE id = ?", [(i,) for i in ids])


def selected_modules(conn: sqlite3.Connection) -> list[Module]:
    return [_module(r) for r in conn.execute("SELECT * FROM modules WHERE selected = 1 ORDER BY name")]


# --- resources --------------------------------------------------------------------

def get_resource(conn: sqlite3.Connection, key: str) -> Resource | None:
    row = conn.execute("SELECT * FROM resources WHERE key = ?", (key,)).fetchone()
    return _resource(row) if row else None


def see_resource(conn: sqlite3.Connection, *, key: str, module_id: int, section: str | None, title: str,
                 resource_type: str, source_url: str, at: str) -> tuple[Resource, bool]:
    """Record that a resource is on Moodle now. Returns ``(resource, reappeared)``.

    New resources start as ``pending``. Known ones get their title, section and
    ``last_seen_at`` refreshed; a ``removed`` one that is back is restored.
    """
    existing = get_resource(conn, key)
    if existing is None:
        conn.execute(
            "INSERT INTO resources (key, module_id, section, title, resource_type, cmid, source_url, status,"
            " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, module_id, section, title, resource_type, cmid(source_url), source_url, PENDING, at, at),
        )
        return get_resource(conn, key), False  # type: ignore[return-value]
    reappeared = existing.status == REMOVED
    status = existing.status
    if reappeared:
        status = DOWNLOADED if existing.content_hash else PENDING
    conn.execute(
        "UPDATE resources SET module_id = ?, section = ?, title = ?, resource_type = ?, source_url = ?,"
        " status = ?, last_seen_at = ? WHERE id = ?",
        (module_id, section, title, resource_type, source_url, status, at, existing.id),
    )
    return get_resource(conn, key), reappeared  # type: ignore[return-value]


def save_download(conn: sqlite3.Connection, resource_id: int, *, file_url: str | None, etag: str | None,
                  last_modified: str | None, local_path: str, file_size: int, content_hash: str,
                  at: str, changed: bool) -> None:
    """Record a file now on disk. ``changed`` marks a genuine content change (NEW or UPDATED)."""
    conn.execute(
        "UPDATE resources SET file_url = ?, etag = ?, last_modified = ?, local_path = ?, file_size = ?,"
        " content_hash = ?, status = ?, last_error = NULL, downloaded_at = ?,"
        " updated_at = CASE WHEN ? THEN ? ELSE updated_at END WHERE id = ?",
        (file_url, etag, last_modified, local_path, file_size, content_hash, DOWNLOADED, at, changed, at,
         resource_id),
    )


def save_unchanged(conn: sqlite3.Connection, resource_id: int, *, file_url: str | None = None,
                   etag: str | None = None, last_modified: str | None = None) -> None:
    """Record an unchanged file, refreshing any newer URL or validators the server gave."""
    conn.execute(
        "UPDATE resources SET file_url = COALESCE(?, file_url), etag = COALESCE(?, etag),"
        " last_modified = COALESCE(?, last_modified), last_error = NULL WHERE id = ?",
        (file_url, etag, last_modified, resource_id),
    )


def save_status(conn: sqlite3.Connection, resource_id: int, status: str) -> None:
    """Set a non-file status such as ``linked`` or ``video_link``; keeps any downloaded file's record."""
    conn.execute(
        "UPDATE resources SET status = CASE WHEN content_hash IS NULL THEN ? ELSE status END,"
        " last_error = NULL WHERE id = ?",
        (status, resource_id),
    )


def save_failure(conn: sqlite3.Connection, resource_id: int, message: str) -> None:
    """Note a failure without disturbing anything previously stored."""
    conn.execute("UPDATE resources SET last_error = ? WHERE id = ?", (message, resource_id))


def mark_unseen_removed(conn: sqlite3.Connection, module_id: int, seen_since: str) -> list[Resource]:
    """Mark a module's resources not seen since ``seen_since`` as removed.

    Call only after a complete, error-free scan of that module. Files are kept.
    """
    rows = conn.execute(
        "SELECT * FROM resources WHERE module_id = ? AND last_seen_at < ? AND status != ?",
        (module_id, seen_since, REMOVED),
    ).fetchall()
    conn.executemany("UPDATE resources SET status = ? WHERE id = ?", [(REMOVED, r["id"]) for r in rows])
    return [_resource(r) for r in rows]


def module_resources(conn: sqlite3.Connection, module_id: int) -> list[Resource]:
    return [_resource(r) for r in conn.execute(
        "SELECT * FROM resources WHERE module_id = ? ORDER BY section, title", (module_id,))]


# --- runs and events ---------------------------------------------------------------

def start_run(conn: sqlite3.Connection, mode: str, at: str) -> int:
    return conn.execute(
        "INSERT INTO sync_runs (started_at, mode, outcome) VALUES (?, ?, 'running')", (at, mode)
    ).lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, outcome: str, at: str, *,
               bytes_received: int = 0, selected_count: int = 0, scanned_count: int = 0) -> None:
    conn.execute("UPDATE sync_runs SET finished_at = ?, outcome = ?, bytes_received = ?, "
                 "selected_count = ?, scanned_count = ? WHERE id = ?",
                 (at, outcome, bytes_received, selected_count, scanned_count, run_id))


def log_event(conn: sqlite3.Connection, run_id: int, resource_id: int, kind: str, at: str, *,
              old_hash: str | None = None, new_hash: str | None = None, archived_path: str | None = None,
              message: str | None = None) -> None:
    conn.execute(
        "INSERT INTO sync_events (run_id, resource_id, kind, old_hash, new_hash, archived_path, message,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, resource_id, kind, old_hash, new_hash, archived_path, message, at),
    )


def events_since(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Changes since a time, with module and resource names: "what's new since yesterday?"."""
    return conn.execute(
        "SELECT e.created_at, e.kind, m.name AS module, r.section, r.title, r.local_path, e.message"
        " FROM sync_events e JOIN resources r ON r.id = e.resource_id JOIN modules m ON m.id = r.module_id"
        " WHERE e.created_at >= ? ORDER BY m.name, e.created_at, e.id",
        (since,),
    ).fetchall()


# --- index.csv import / export -------------------------------------------------------

LEGACY_FIELDS = ("module", "section", "title", "type", "url", "status", "local_file")
EXPORT_FIELDS = LEGACY_FIELDS + ("first_seen_at", "updated_at", "last_error")
_LEGACY_DIGEST = re.compile(r"-([0-9a-f]{8})$")


@dataclass
class ImportReport:
    imported: int = 0
    skipped_existing: int = 0
    hashed: int = 0
    missing_files: list[str] = field(default_factory=list)
    unindexed_files: list[str] = field(default_factory=list)


def import_legacy_index(conn: sqlite3.Connection, root: Path, index_csv: Path, at: str) -> ImportReport:
    """One-off import of organiser.py's index.csv as the baseline (no NEW events).

    Existing local files are hashed so the first sync can recognise them.
    Resources already in the database are left alone, so it is safe to rerun.
    Files under materials/ that the index doesn't mention are reported, never deleted.
    """
    report = ImportReport()
    if not index_csv.is_file():
        return report
    with index_csv.open(newline="", encoding="utf-8-sig") as handle:
        rows = [r for r in csv.DictReader(handle) if r.get("module") and r.get("url")]
    referenced: set[Path] = set()
    with conn:
        for row in rows:
            key = resource_key(row["url"])
            if get_resource(conn, key) is not None:
                report.skipped_existing += 1
                continue
            module = upsert_module(conn, moodle_id=None, name=row["module"], url=None, at=at)
            _import_row(conn, root, row, key, module.id, at, report, referenced)
            report.imported += 1
    report.unindexed_files = _unindexed(root / "materials", referenced, root)
    return report


def _import_row(conn: sqlite3.Connection, root: Path, row: dict[str, str], key: str, module_id: int, at: str,
                report: ImportReport, referenced: set[Path]) -> None:
    url, legacy_status, local = row["url"], row.get("status", ""), row.get("local_file", "")
    is_file_url = urlparse(url).path.startswith("/pluginfile.php/")
    resource_type = "folder_file" if row.get("type") == "folder" and is_file_url else (row.get("type") or "file")
    values: dict[str, object] = {"status": PENDING, "file_url": url if is_file_url else None}

    if legacy_status == "downloaded" and local:
        try:
            path = resolve_local(root, local)
        except ValueError:
            path = None
        if path is not None and path.is_file():
            referenced.add(path)
            digest = _LEGACY_DIGEST.search(path.stem)
            values.update(status=DOWNLOADED, local_path=local, file_size=path.stat().st_size,
                          content_hash=sha256_file(path), downloaded_at=at,
                          legacy_url_digest=digest.group(1) if digest else None)
            report.hashed += 1
        else:
            report.missing_files.append(local)
    elif legacy_status == "linked video":
        values["status"] = VIDEO_LINK
    elif legacy_status.startswith("error"):
        values["last_error"] = legacy_status
    elif legacy_status.startswith("linked"):
        values["status"] = LINKED

    columns = {"key": key, "module_id": module_id, "section": row.get("section"), "title": row["title"],
               "resource_type": resource_type, "cmid": cmid(url), "source_url": url,
               "first_seen_at": at, "last_seen_at": at, **values}
    names = ", ".join(columns)
    conn.execute(f"INSERT INTO resources ({names}) VALUES ({', '.join('?' * len(columns))})",
                 tuple(columns.values()))


def _unindexed(materials: Path, referenced: set[Path], root: Path) -> list[str]:
    if not materials.is_dir():
        return []
    skip = {INCOMING, VERSIONS}
    found = []
    for path in sorted(materials.rglob("*")):
        if not path.is_file() or path.name == "index.csv" or skip & set(path.relative_to(materials).parts):
            continue
        if path.resolve() not in referenced:
            found.append(str(path.relative_to(root)))
    return found


def export_index_csv(conn: sqlite3.Connection, path: Path) -> int:
    """Write the browsable index.csv from the database. Returns the row count."""
    rows = conn.execute(
        "SELECT m.name AS module, r.section, r.title, r.resource_type AS type, r.source_url AS url, r.status,"
        " COALESCE(r.local_path, '') AS local_file, r.first_seen_at, COALESCE(r.updated_at, '') AS updated_at,"
        " COALESCE(r.last_error, '') AS last_error"
        " FROM resources r JOIN modules m ON m.id = r.module_id ORDER BY m.name, r.section, r.title"
    ).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".csv.tmp")
    with temp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows(dict(r) for r in rows)
    temp.replace(path)
    return len(rows)
