"""Keep the library index in step with downloaded files, and query it.

A file is (re)read only when its content hash differs from the one it was
indexed at, so running this after every sync is cheap.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from ..db import DOWNLOADED, REMOVED, utc_now
from ..moodle.storage import resolve_local
from .dates import find_dates
from .extract import SUPPORTED, Document, UnsupportedFile, extract


@dataclass
class IndexReport:
    indexed: int = 0
    unchanged: int = 0
    unsupported: int = 0
    failed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchHit:
    resource_id: int
    module: str
    section: str | None
    title: str
    page: int
    page_title: str
    snippet: str             # with [[ and ]] around matched words
    local_path: str | None


def index_library(conn: sqlite3.Connection, root, *, today: date | None = None, rebuild: bool = False,
                  progress: Callable[[str], None] | None = None) -> IndexReport:
    """Read every downloaded file whose content changed since it was last indexed."""
    today = today or date.today()
    report = IndexReport()
    rows = conn.execute(
        "SELECT r.id, r.title, r.local_path, r.content_hash, d.content_hash AS indexed_hash"
        " FROM resources r LEFT JOIN documents d ON d.resource_id = r.id"
        " WHERE r.status IN (?, ?) AND r.local_path IS NOT NULL AND r.content_hash IS NOT NULL",
        (DOWNLOADED, REMOVED),
    ).fetchall()
    for row in rows:
        if not rebuild and row["indexed_hash"] == row["content_hash"]:
            report.unchanged += 1
            continue
        try:
            path = resolve_local(root, row["local_path"])
        except ValueError:
            continue
        if path.suffix.lower() not in SUPPORTED or not path.is_file():
            report.unsupported += 1
            _store(conn, row["id"], row["content_hash"], None, "Not a readable document type", today)
            continue
        if progress:
            progress(row["title"])
        try:
            document = extract(path)
        except UnsupportedFile as exc:
            report.unsupported += 1
            _store(conn, row["id"], row["content_hash"], None, str(exc), today)
            continue
        except Exception as exc:  # a damaged file must not stop the rest
            report.failed.append(f"{row['title']}: {type(exc).__name__}")
            _store(conn, row["id"], row["content_hash"], None, f"{type(exc).__name__}: {exc}", today)
            continue
        _store(conn, row["id"], row["content_hash"], document, None, today)
        report.indexed += 1
    return report


def _store(conn: sqlite3.Connection, resource_id: int, content_hash: str, document: Document | None,
           error: str | None, today: date) -> None:
    with conn:
        conn.execute("DELETE FROM page_search WHERE resource_id = ?", (resource_id,))
        conn.execute("DELETE FROM date_mentions WHERE resource_id = ?", (resource_id,))
        conn.execute(
            "INSERT OR REPLACE INTO documents (resource_id, content_hash, kind, page_count, word_count, outline,"
            " truncated, error, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (resource_id, content_hash, document.kind if document else None,
             len(document.pages) if document else 0, document.word_count if document else 0,
             json.dumps(document.outline() if document else []), int(document.truncated) if document else 0,
             error, utc_now()),
        )
        if document is None:
            return
        conn.executemany(
            "INSERT INTO page_search (title, body, resource_id, page) VALUES (?, ?, ?, ?)",
            [(p.title, p.text, resource_id, p.number) for p in document.pages if p.text or p.title],
        )
        mentions = []
        for page in document.pages:
            for mention in find_dates(page.text, today=today):
                mentions.append((resource_id, mention.on.isoformat(), mention.kind, page.number, mention.snippet))
        conn.executemany(
            "INSERT INTO date_mentions (resource_id, on_date, kind, page, snippet) VALUES (?, ?, ?, ?, ?)",
            mentions)


# --- queries -------------------------------------------------------------------------

def search(conn: sqlite3.Connection, query: str, *, limit: int = 40) -> list[SearchHit]:
    """Full-text search across every page; best matches first."""
    match = fts_query(query)
    if not match:
        return []
    rows = conn.execute(
        "SELECT s.resource_id, s.page, s.title AS page_title,"
        " snippet(page_search, 1, '[[', ']]', '…', 18) AS snippet,"
        " r.title, r.section, r.local_path, m.name AS module"
        " FROM page_search s JOIN resources r ON r.id = s.resource_id JOIN modules m ON m.id = r.module_id"
        " WHERE page_search MATCH ? ORDER BY bm25(page_search, 5.0, 1.0) LIMIT ?",
        (match, limit),
    ).fetchall()
    return [SearchHit(r["resource_id"], r["module"], r["section"], r["title"], int(r["page"]), r["page_title"],
                      r["snippet"], r["local_path"]) for r in rows]


def fts_query(text: str) -> str:
    """Turn what someone typed into a safe FTS5 query: all words, last one as a prefix."""
    words = re.findall(r"\w+", text, flags=re.UNICODE)
    if not words:
        return ""
    parts = [f'"{w}"' for w in words[:-1]] + [f'"{words[-1]}"*']
    return " ".join(parts)


def dates_between(conn: sqlite3.Connection, start: date, end: date | None = None,
                  kinds: tuple[str, ...] = ("deadline", "exam", "date")) -> list[sqlite3.Row]:
    """Dates found in files, soonest first, with their module and file."""
    end_text = (end or date(9999, 12, 31)).isoformat()
    marks = ",".join("?" * len(kinds))
    return conn.execute(
        "SELECT d.on_date, d.kind, d.page, d.snippet, r.id AS resource_id, r.title, r.section, r.local_path,"
        " m.name AS module FROM date_mentions d JOIN resources r ON r.id = d.resource_id"
        " JOIN modules m ON m.id = r.module_id"
        f" WHERE d.on_date >= ? AND d.on_date <= ? AND d.kind IN ({marks}) AND r.status != ?"
        " ORDER BY d.on_date, d.kind, m.name",
        (start.isoformat(), end_text, *kinds, REMOVED),
    ).fetchall()


def outline(conn: sqlite3.Connection, resource_id: int) -> list[tuple[int, str]]:
    row = conn.execute("SELECT outline FROM documents WHERE resource_id = ?", (resource_id,)).fetchone()
    return [tuple(item) for item in json.loads(row["outline"])] if row else []
