"""The flashcard queue: what's due, and recording each answer."""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from ..db import utc_now
from .srs import CardState, review


def due_cards(conn: sqlite3.Connection, today: date, *, module_id: int | None = None, limit: int = 50) -> list[dict]:
    """Cards due today or earlier (oldest due first), with where they came from."""
    rows = conn.execute(
        "SELECT c.id, c.question, c.answer, c.due_on, c.reps, c.lapses, r.id AS resource_id, r.title,"
        " m.id AS module_id, m.name AS module FROM flashcards c JOIN resources r ON r.id = c.resource_id"
        " JOIN modules m ON m.id = r.module_id"
        " WHERE c.due_on <= ? AND (? IS NULL OR m.id = ?) ORDER BY c.due_on, c.reps, c.id LIMIT ?",
        (today.isoformat(), module_id, module_id, limit),
    ).fetchall()
    return [{**dict(r), "module": re.sub(r"\s*\(.*$", "", r["module"]).strip()} for r in rows]


def due_count(conn: sqlite3.Connection, today: date) -> int:
    return conn.execute("SELECT COUNT(*) FROM flashcards WHERE due_on <= ?", (today.isoformat(),)).fetchone()[0]


def card_totals(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Per module: cards in total, due now, and learned (seen at least twice successfully)."""
    rows = conn.execute(
        "SELECT m.id, m.name, COUNT(c.id) AS total, SUM(c.due_on <= ?) AS due, SUM(c.reps >= 2) AS learned"
        " FROM flashcards c JOIN resources r ON r.id = c.resource_id JOIN modules m ON m.id = r.module_id"
        " GROUP BY m.id ORDER BY m.name",
        (today.isoformat(),),
    ).fetchall()
    return [{"module_id": r["id"], "module": re.sub(r"\s*\(.*$", "", r["name"]).strip(), "total": r["total"],
             "due": r["due"] or 0, "learned": r["learned"] or 0} for r in rows]


def record_review(conn: sqlite3.Connection, card_id: int, grade: str, today: date) -> dict:
    """Apply an answer to a card and return its new schedule."""
    row = conn.execute("SELECT * FROM flashcards WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        raise KeyError(card_id)
    state = review(CardState(row["interval_days"], row["ease"], row["reps"], row["lapses"]), grade, today)
    with conn:
        conn.execute(
            "UPDATE flashcards SET interval_days = ?, ease = ?, reps = ?, lapses = ?, due_on = ?, last_reviewed_at = ?"
            " WHERE id = ?",
            (state.interval_days, state.ease, state.reps, state.lapses, state.due_on.isoformat(), utc_now(), card_id))
    return {"id": card_id, "due_on": state.due_on.isoformat(), "interval_days": state.interval_days}
