"""Study notes and flashcards from a file's text, for free.

Two routes, both stored the same way:

* **On this Mac:** Ollama (https://ollama.com) runs an open model locally.
  Free and private; nothing leaves the computer.
* **Copy for Claude:** :func:`claude_prompt` builds a request to paste into
  Claude on your existing plan; :func:`parse_reply` reads Claude's answer back.

Notes are tied to the file's content hash, so a file is only summarised again
when the lecturer changes it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from ..db import DOWNLOADED, utc_now

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma3:4b"
MAX_CHARS_LOCAL = 24_000     # ~6k tokens: what a small local model handles well
MAX_CHARS_CLAUDE = 150_000   # Claude reads far more
MAX_PAGES_AUTO = 80          # longer files (reference books) are skipped unless asked for
MIN_WORDS = 40               # less than this isn't worth summarising

SYSTEM = (
    "You help a UK university engineering student revise. Use only the text you are given. "
    "Write in plain British English. Keep formulas as written, with units. Never invent facts."
)
TASK = (
    "From the course material below, write:\n"
    "1. A summary of 3 to 5 sentences: what this material covers and why it matters.\n"
    "2. 5 to 8 key points: the facts, definitions, equations or methods worth remembering.\n"
    "3. 6 to 10 flashcards that test understanding: a specific question and a short, exact answer."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "flashcards": {"type": "array", "items": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
            "required": ["question", "answer"]}},
    },
    "required": ["summary", "key_points", "flashcards"],
}


class NotesError(Exception):
    """Notes couldn't be produced or read; the message says what to do."""


@dataclass
class Notes:
    summary: str
    key_points: list[str]
    flashcards: list[tuple[str, str]]
    source: str                         # ollama | claude
    model: str | None = None
    truncated: bool = False


# --- the file's text --------------------------------------------------------------------

def document_text(conn: sqlite3.Connection, resource_id: int, max_chars: int) -> tuple[str, bool]:
    """The file's text from the search index, page by page. Returns ``(text, truncated)``."""
    rows = conn.execute("SELECT page, title, body FROM page_search WHERE resource_id = ? ORDER BY CAST(page AS INTEGER)",
                        (resource_id,)).fetchall()
    parts, total = [], 0
    for row in rows:
        chunk = f"[Page {row['page']}] {row['body']}".strip()
        if total + len(chunk) > max_chars:
            parts.append(chunk[: max(0, max_chars - total)])
            return "\n\n".join(parts), True
        parts.append(chunk)
        total += len(chunk) + 2
    return "\n\n".join(parts), False


def _heading(conn: sqlite3.Connection, resource_id: int) -> tuple[str, str]:
    row = conn.execute("SELECT r.title, m.name FROM resources r JOIN modules m ON m.id = r.module_id WHERE r.id = ?",
                       (resource_id,)).fetchone()
    if row is None:
        raise NotesError("That file isn't in the library")
    return row["title"], re.sub(r"\s*\(.*$", "", row["name"]).strip()


# --- route 1: Ollama on this Mac --------------------------------------------------------------

Post = Callable[[str, dict, float], dict]


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # local Ollama only
        return json.loads(response.read())


def _get_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


@dataclass
class Ollama:
    """Talks to the Ollama app on this computer (it listens on 127.0.0.1:11434)."""

    model: str | None = None
    base_url: str = OLLAMA_URL
    timeout: float = 900.0
    post: Post = field(default=_post_json, repr=False)
    get: Callable[[str, float], dict] = field(default=_get_json, repr=False)

    def installed_models(self) -> list[str]:
        try:
            return [m["name"] for m in self.get(f"{self.base_url}/api/tags", 3.0).get("models", [])]
        except (urllib.error.URLError, OSError, ValueError):
            return []

    def status(self) -> dict:
        """What the dashboard shows: is Ollama running, and which model will be used."""
        models = self.installed_models()
        return {"running": bool(models) or self._reachable(), "models": models, "model": self.pick_model(models)}

    def _reachable(self) -> bool:
        try:
            self.get(f"{self.base_url}/api/version", 3.0)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def pick_model(self, models: list[str] | None = None) -> str | None:
        models = self.installed_models() if models is None else models
        wanted = self.model or DEFAULT_MODEL
        for name in models:
            if name == wanted or name.split(":")[0] == wanted.split(":")[0] and ":" not in wanted:
                return name
        if self.model:
            return None  # asked for a specific model that isn't installed
        return models[0] if models else None

    def make_notes(self, title: str, module: str, text: str, *, truncated: bool = False) -> Notes:
        model = self.pick_model()
        if model is None:
            raise NotesError(f"No AI model installed. In Terminal run: ollama pull {self.model or DEFAULT_MODEL}")
        prompt = f"{TASK}\n\nModule: {module}\nFile: {title}\n\n---\n{text}\n---"
        try:
            reply = self.post(f"{self.base_url}/api/chat", {
                "model": model,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                "format": SCHEMA, "stream": False, "options": {"num_ctx": 16384, "temperature": 0.2},
            }, self.timeout)
        except (urllib.error.URLError, OSError) as exc:
            raise NotesError("Ollama isn't running. Open the Ollama app, then try again.") from exc
        try:
            data = json.loads(reply["message"]["content"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NotesError("The model's answer couldn't be read; try again or use a bigger model") from exc
        notes = _from_dict(data, source="ollama", model=model)
        notes.truncated = truncated
        return notes


def _from_dict(data: dict, *, source: str, model: str | None) -> Notes:
    summary = str(data.get("summary", "")).strip()
    points = [str(p).strip() for p in data.get("key_points", []) if str(p).strip()]
    cards = []
    for card in data.get("flashcards", []):
        if isinstance(card, dict):
            q, a = str(card.get("question", "")).strip(), str(card.get("answer", "")).strip()
            if q and a:
                cards.append((q, a))
    if not summary and not cards:
        raise NotesError("The answer had no summary or flashcards")
    return Notes(summary, points, cards, source, model)


# --- route 2: copy for Claude, paste the reply back ---------------------------------------------

def claude_prompt(conn: sqlite3.Connection, resource_id: int) -> str:
    """A ready-to-paste request for Claude, with the file's text and the reply format we can read back."""
    title, module = _heading(conn, resource_id)
    text, truncated = document_text(conn, resource_id, MAX_CHARS_CLAUDE)
    if not text.strip():
        raise NotesError("This file has no readable text")
    note = "\n(The file is long; this is its first part.)" if truncated else ""
    return (
        f"{SYSTEM}\n\n{TASK}\n\n"
        "Reply in exactly this format, so my study app can read it:\n\n"
        "SUMMARY:\n<the summary>\n\nKEY POINTS:\n- <point>\n- <point>\n\n"
        "FLASHCARDS:\nQ: <question>\nA: <answer>\n\nQ: <question>\nA: <answer>\n\n"
        f"Module: {module}\nFile: {title}{note}\n\n---\n{text}\n---"
    )


def parse_reply(text: str) -> Notes:
    """Read Claude's reply in the SUMMARY / KEY POINTS / FLASHCARDS format."""
    text = text.replace("\r\n", "\n").replace("**", "")
    sections = {name: "" for name in ("SUMMARY", "KEY POINTS", "FLASHCARDS")}
    pattern = re.compile(r"^\s*#*\s*(SUMMARY|KEY POINTS|FLASHCARDS)\s*:?\s*$", re.I | re.M)
    marks = list(pattern.finditer(text))
    for n, mark in enumerate(marks):
        end = marks[n + 1].start() if n + 1 < len(marks) else len(text)
        sections[mark.group(1).upper()] = text[mark.end():end].strip()
    points = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
              for line in sections["KEY POINTS"].splitlines() if line.strip()]
    cards = []
    for match in re.finditer(r"^\s*Q\s*[:.]\s*(.+?)\n\s*A\s*[:.]\s*(.+?)(?=\n\s*Q\s*[:.]|\Z)",
                             sections["FLASHCARDS"], re.S | re.M):
        q, a = (re.sub(r"\s+", " ", part).strip() for part in match.groups())
        if q and a:
            cards.append((q, a))
    summary = re.sub(r"\s+", " ", sections["SUMMARY"]).strip()
    if not summary and not cards:
        raise NotesError("Couldn't find SUMMARY or FLASHCARDS in the pasted text. Paste Claude's whole reply.")
    return Notes(summary, points, cards, "claude", None)


# --- storage ----------------------------------------------------------------------------------

def save_notes(conn: sqlite3.Connection, resource_id: int, notes: Notes, today: date | None = None) -> int:
    """Store notes and their flashcards, replacing earlier ones for this file. Returns cards added."""
    today = today or date.today()
    row = conn.execute("SELECT content_hash FROM resources WHERE id = ?", (resource_id,)).fetchone()
    if row is None or not row["content_hash"]:
        raise NotesError("That file hasn't been downloaded")
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO ai_notes (resource_id, content_hash, source, model, summary, key_points, truncated,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (resource_id, row["content_hash"], notes.source, notes.model, notes.summary,
             json.dumps(notes.key_points), int(notes.truncated), utc_now()))
        conn.execute("DELETE FROM flashcards WHERE resource_id = ?", (resource_id,))
        conn.executemany(
            "INSERT INTO flashcards (resource_id, question, answer, source, created_at, due_on) VALUES (?, ?, ?, ?, ?, ?)",
            [(resource_id, q, a, notes.source, utc_now(), today.isoformat()) for q, a in notes.flashcards])
    return len(notes.flashcards)


def make_notes_for(conn: sqlite3.Connection, resource_id: int, ollama: Ollama) -> Notes:
    """Summarise one file with Ollama and store the result."""
    title, module = _heading(conn, resource_id)
    text, truncated = document_text(conn, resource_id, MAX_CHARS_LOCAL)
    if len(text.split()) < MIN_WORDS:
        raise NotesError("This file has too little text to summarise")
    notes = ollama.make_notes(title, module, text, truncated=truncated)
    save_notes(conn, resource_id, notes)
    return notes


def files_needing_notes(conn: sqlite3.Connection, *, include_long: bool = False) -> list[sqlite3.Row]:
    """Downloaded, readable files without notes for their current version, newest first."""
    return conn.execute(
        "SELECT r.id, r.title, d.page_count FROM resources r JOIN documents d ON d.resource_id = r.id"
        " LEFT JOIN ai_notes n ON n.resource_id = r.id AND n.content_hash = r.content_hash"
        " WHERE r.status = ? AND d.error IS NULL AND d.word_count >= ? AND n.resource_id IS NULL"
        " AND (? OR d.page_count <= ?)"
        " ORDER BY COALESCE(r.updated_at, r.first_seen_at) DESC, r.id DESC",
        (DOWNLOADED, MIN_WORDS, int(include_long), MAX_PAGES_AUTO),
    ).fetchall()


def get_notes(conn: sqlite3.Connection, resource_id: int) -> dict | None:
    row = conn.execute("SELECT n.*, r.content_hash AS current_hash FROM ai_notes n JOIN resources r"
                       " ON r.id = n.resource_id WHERE n.resource_id = ?", (resource_id,)).fetchone()
    if row is None:
        return None
    cards = conn.execute("SELECT id, question, answer, due_on, reps FROM flashcards WHERE resource_id = ? ORDER BY id",
                         (resource_id,)).fetchall()
    return {"summary": row["summary"], "key_points": json.loads(row["key_points"]), "source": row["source"],
            "model": row["model"], "truncated": bool(row["truncated"]), "created_at": row["created_at"],
            "outdated": row["content_hash"] != row["current_hash"],
            "flashcards": [dict(c) for c in cards]}


# --- all files at once ---------------------------------------------------------------------

@dataclass
class BatchProgress:
    """Shared between the background worker and whoever is watching it."""

    total: int = 0
    done: int = 0
    current: str = ""
    errors: list[str] = field(default_factory=list)
    running: bool = False
    stopped: bool = False
    stop: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_dict(self) -> dict:
        return {"total": self.total, "done": self.done, "current": self.current, "errors": self.errors[-5:],
                "running": self.running, "stopped": self.stopped}


def summarise_all(connect: Callable[[], sqlite3.Connection], ollama: Ollama, progress: BatchProgress, *,
                  include_long: bool = False, limit: int | None = None) -> BatchProgress:
    """Make notes for every file that needs them, newest first. Each file is saved as it finishes,
    so stopping part-way loses nothing."""
    conn = connect()
    try:
        todo = files_needing_notes(conn, include_long=include_long)[:limit]
        progress.total, progress.running = len(todo), True
        for row in todo:
            if progress.stop.is_set():
                progress.stopped = True
                break
            progress.current = row["title"]
            try:
                make_notes_for(conn, row["id"], ollama)
            except NotesError as exc:
                progress.errors.append(f"{row['title']}: {exc}")
                if "Ollama isn't running" in str(exc) or "No AI model" in str(exc):
                    break  # every other file would fail the same way
            progress.done += 1
    finally:
        progress.running, progress.current = False, ""
        conn.close()
    return progress


def run_in_background(connect: Callable[[], sqlite3.Connection], ollama: Ollama) -> BatchProgress:
    progress = BatchProgress(running=True)
    threading.Thread(target=summarise_all, args=(connect, ollama, progress), daemon=True).start()
    time.sleep(0.05)
    return progress
