"""Data for the dashboard, shaped as plain dicts ready to send as JSON. No HTTP here."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath

from .. import db
from ..library.cards import due_count
from ..library.index import dates_between, search
from ..library.notes import get_notes

NEW_DAYS = 7
FEED_DAYS = 14
AHEAD_DAYS = 120
_CODE = re.compile(r"\b([A-Z]{4}\d{4})\b")


def short_module_name(name: str) -> tuple[str, str]:
    """('Architectural Engineering Design 3', 'ABEE2014') from Moodle's long course name."""
    short = re.sub(r"\s*\(.*$", "", name).strip() or name
    codes = _CODE.findall(name)
    return short, codes[0] if codes else ""


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kind(row: sqlite3.Row) -> str:
    """What the item is, for its icon: pdf, pptx, docx, ..., video, link."""
    if row["status"] == db.VIDEO_LINK:
        return "video"
    if row["local_path"]:
        suffix = PurePosixPath(row["local_path"]).suffix.lower().lstrip(".")
        return suffix or "file"
    return "link"


# --- views ---------------------------------------------------------------------------

def overview(conn: sqlite3.Connection, today: date) -> dict:
    modules = []
    for m in conn.execute(
        "SELECT m.id, m.name, COUNT(r.id) AS files,"
        " SUM(CASE WHEN r.updated_at >= ? THEN 1 ELSE 0 END) AS recent"
        " FROM modules m LEFT JOIN resources r ON r.module_id = m.id AND r.status = ?"
        " WHERE m.selected = 1 GROUP BY m.id ORDER BY m.name",
        (_iso_days_ago(NEW_DAYS), db.DOWNLOADED),
    ):
        short, code = short_module_name(m["name"])
        modules.append({"id": m["id"], "name": short, "code": code, "full_name": m["name"],
                        "files": m["files"], "recent": m["recent"] or 0})
    return {"today": today.isoformat(), "modules": modules, "coming_up": coming_up(conn, today),
            "feed": feed(conn), "cards_due": due_count(conn, today)}


def coming_up(conn: sqlite3.Connection, today: date, days: int = AHEAD_DAYS) -> list[dict]:
    """Deadlines and exams found in files, soonest first, one per date and file."""
    seen: set[tuple[str, int, str]] = set()
    items = []
    for row in dates_between(conn, today, today + timedelta(days=days), kinds=("deadline", "exam")):
        key = (row["on_date"], row["resource_id"], row["kind"])
        if key in seen:
            continue
        seen.add(key)
        short, code = short_module_name(row["module"])
        items.append({"date": row["on_date"], "kind": row["kind"], "snippet": row["snippet"],
                      "page": row["page"], "resource_id": row["resource_id"], "title": row["title"],
                      "module": short, "code": code})
    return items


def feed(conn: sqlite3.Connection, days: int = FEED_DAYS) -> list[dict]:
    rows = conn.execute(
        "SELECT e.created_at, e.kind, r.id AS resource_id, r.title, r.section, r.status, r.local_path,"
        " m.name AS module FROM sync_events e JOIN resources r ON r.id = e.resource_id"
        " JOIN modules m ON m.id = r.module_id"
        " WHERE e.created_at >= ? AND e.kind IN ('new', 'updated', 'removed')"
        " ORDER BY e.created_at DESC, e.id DESC LIMIT 400",
        (_iso_days_ago(days),),
    ).fetchall()
    return [{"at": r["created_at"], "event": r["kind"], "resource_id": r["resource_id"], "title": r["title"],
             "section": r["section"], "module": short_module_name(r["module"])[0], "kind": _kind(r)}
            for r in rows]


def module_detail(conn: sqlite3.Connection, module_id: int) -> dict | None:
    module = conn.execute("SELECT * FROM modules WHERE id = ?", (module_id,)).fetchone()
    if module is None:
        return None
    recent = _iso_days_ago(NEW_DAYS)
    sections: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT r.*, d.page_count, d.error AS read_error FROM resources r"
        " LEFT JOIN documents d ON d.resource_id = r.id"
        " WHERE r.module_id = ? AND r.resource_type != 'folder' AND r.status != ? ORDER BY r.id",
        (module_id, db.REMOVED),
    ):
        badge = ""
        if r["updated_at"] and r["updated_at"] >= recent:
            badge = "new" if r["first_seen_at"] >= recent else "updated"
        sections.setdefault(r["section"] or "General", []).append({
            "id": r["id"], "title": r["title"], "type": r["resource_type"], "status": r["status"],
            "kind": _kind(r), "pages": r["page_count"], "badge": badge, "moodle_url": r["source_url"],
            "readable": bool(r["page_count"]) and not r["read_error"],
        })
    short, code = short_module_name(module["name"])
    return {"id": module["id"], "name": short, "code": code, "full_name": module["name"], "url": module["url"],
            "sections": [{"name": name, "items": items} for name, items in sections.items()]}


def resource_detail(conn: sqlite3.Connection, resource_id: int, today: date) -> dict | None:
    r = conn.execute(
        "SELECT r.*, m.name AS module, d.kind AS doc_kind, d.page_count, d.word_count, d.outline,"
        " d.truncated, d.error AS read_error FROM resources r JOIN modules m ON m.id = r.module_id"
        " LEFT JOIN documents d ON d.resource_id = r.id WHERE r.id = ?",
        (resource_id,),
    ).fetchone()
    if r is None:
        return None
    dates = conn.execute(
        "SELECT on_date, kind, page, snippet FROM date_mentions WHERE resource_id = ? ORDER BY on_date, page",
        (resource_id,),
    ).fetchall()
    versions = conn.execute(
        "SELECT created_at, archived_path FROM sync_events WHERE resource_id = ? AND kind = 'updated'"
        " AND archived_path IS NOT NULL ORDER BY created_at DESC",
        (resource_id,),
    ).fetchall()
    short, code = short_module_name(r["module"])
    return {
        "id": r["id"], "title": r["title"], "module": short, "module_id": r["module_id"], "code": code,
        "section": r["section"], "status": r["status"], "kind": _kind(r), "moodle_url": r["source_url"],
        "has_file": bool(r["local_path"]) and r["status"] in (db.DOWNLOADED, db.REMOVED),
        "first_seen_at": r["first_seen_at"], "updated_at": r["updated_at"],
        "pages": r["page_count"], "words": r["word_count"], "truncated": bool(r["truncated"]),
        "read_error": r["read_error"],
        "outline": [{"page": p, "title": t} for p, t in json.loads(r["outline"] or "[]")],
        "dates": [{"date": d["on_date"], "kind": d["kind"], "page": d["page"], "snippet": d["snippet"],
                   "upcoming": d["on_date"] >= today.isoformat()} for d in dates],
        "versions": [{"at": v["created_at"], "path": v["archived_path"]} for v in versions],
        "notes": get_notes(conn, resource_id),
        "readable": bool(r["page_count"]) and not r["read_error"],
    }


def search_results(conn: sqlite3.Connection, query: str) -> list[dict]:
    return [{"resource_id": h.resource_id, "module": short_module_name(h.module)[0], "section": h.section,
             "title": h.title, "page": h.page, "page_title": h.page_title, "snippet": h.snippet,
             "kind": PurePosixPath(h.local_path).suffix.lower().lstrip(".") if h.local_path else "link"}
            for h in search(conn, query)]
