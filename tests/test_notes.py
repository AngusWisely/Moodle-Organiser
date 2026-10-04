"""Free study notes: spaced repetition, Ollama on the Mac (faked), Copy for Claude, flashcard queue."""

import hashlib
import json
import urllib.error
from datetime import date, timedelta

import pytest

from student_os import db
from student_os.library.cards import card_totals, due_cards, due_count, record_review
from student_os.library.index import index_library
from student_os.library.notes import (
    DEFAULT_MODEL,
    Notes,
    NotesError,
    Ollama,
    claude_prompt,
    document_text,
    files_needing_notes,
    get_notes,
    make_notes_for,
    parse_reply,
    save_notes,
)
from student_os.library.srs import CardState, review
from tests.documents import make_pdf

TODAY = date(2026, 10, 4)


# --- spaced repetition ---------------------------------------------------------------

def test_good_answers_space_out_reviews():
    state = CardState()
    gaps = []
    for _ in range(4):
        state = review(state, "good", TODAY)
        gaps.append((state.due_on - TODAY).days)
    assert gaps == [1, 3, 8, 19]


def test_forgetting_resets_and_makes_card_harder():
    state = review(review(review(CardState(), "good", TODAY), "good", TODAY), "again", TODAY)
    assert (state.due_on, state.reps, state.lapses, state.ease) == (TODAY, 0, 1, 2.3)
    assert review(state, "good", TODAY).due_on == TODAY + timedelta(days=1)


def test_easy_and_hard():
    assert review(CardState(), "easy", TODAY).due_on == TODAY + timedelta(days=4)
    hard = review(CardState(interval_days=10, reps=3), "hard", TODAY)
    assert hard.due_on == TODAY + timedelta(days=12) and hard.ease == 2.35
    assert review(CardState(ease=1.35), "again", TODAY).ease == 1.3
    with pytest.raises(ValueError):
        review(CardState(), "meh", TODAY)


# --- a library with one lecture ----------------------------------------------------------

LECTURE_LINES = [["Psychrometry", "Moist air is a mixture of dry air and water vapour.",
                  "Relative humidity compares the vapour pressure with the saturation vapour pressure.",
                  "The dew point is the temperature at which condensation begins."],
                 ["Cooling coils", "The apparatus dew point is the effective coil surface temperature.",
                  "The bypass factor is the fraction of air that passes the coil unaffected.",
                  "Sensible heat ratio equals sensible load divided by total load."]]


@pytest.fixture
def library(tmp_path):
    conn = db.connect(tmp_path / "data" / "moodle.sqlite3")
    module = db.upsert_module(conn, moodle_id=1, name="Architectural Engineering Design 3 (ABEE2014 UNUK AUT)",
                              url=None, at="2026-10-01T00:00:00Z")
    (tmp_path / "materials").mkdir()
    path = make_pdf(tmp_path / "materials" / "psychro.pdf", LECTURE_LINES)
    resource, _ = db.see_resource(conn, key="cm:1", module_id=module.id, section="Week 2", title="Psychrometry Lecture",
                                  resource_type="resource", source_url="https://moodle.nottingham.ac.uk/mod/resource/view.php?id=1",
                                  at="2026-10-01T00:00:00Z")
    db.save_download(conn, resource.id, file_url=None, etag=None, last_modified=None,
                     local_path="materials/psychro.pdf", file_size=path.stat().st_size,
                     content_hash=hashlib.sha256(path.read_bytes()).hexdigest(), at="2026-10-01T00:00:00Z", changed=True)
    conn.commit()
    index_library(conn, tmp_path, today=TODAY)
    yield conn, resource.id
    conn.close()


class FakeOllama:
    """Records requests and answers like Ollama's /api/tags and /api/chat."""

    def __init__(self, models=(DEFAULT_MODEL,), reply=None, down=False):
        self.models, self.down, self.requests = list(models), down, []
        self.reply = reply or {"summary": "Psychrometry describes moist air.",
                               "key_points": ["Dew point is where condensation begins", " "],
                               "flashcards": [{"question": "What is the dew point?", "answer": "Where condensation begins"},
                                              {"question": "Define bypass factor", "answer": "Fraction of air unaffected by the coil"},
                                              {"question": "", "answer": "dropped"}]}

    def get(self, url, timeout):
        if self.down:
            raise urllib.error.URLError("refused")
        return {"models": [{"name": m} for m in self.models]}

    def post(self, url, payload, timeout):
        if self.down:
            raise urllib.error.URLError("refused")
        self.requests.append((url, payload))
        return {"message": {"content": json.dumps(self.reply)}}

    def client(self, **kwargs):
        return Ollama(post=self.post, get=self.get, **kwargs)


def test_document_text_is_ordered_and_capped(library):
    conn, rid = library
    text, truncated = document_text(conn, rid, 10_000)
    assert text.startswith("[Page 1]") and text.index("[Page 2]") > text.index("dew point") and not truncated
    short, truncated = document_text(conn, rid, 50)
    assert len(short) <= 50 and truncated


def test_ollama_notes_are_saved_with_flashcards(library):
    conn, rid = library
    fake = FakeOllama()
    notes = make_notes_for(conn, rid, fake.client())
    url, payload = fake.requests[0]
    assert url.endswith("/api/chat") and payload["model"] == DEFAULT_MODEL and payload["format"]["type"] == "object"
    assert "Bypass factor" in payload["messages"][1]["content"] or "bypass factor" in payload["messages"][1]["content"]
    assert notes.key_points == ["Dew point is where condensation begins"] and len(notes.flashcards) == 2
    stored = get_notes(conn, rid)
    assert stored["summary"].startswith("Psychrometry") and stored["source"] == "ollama" and not stored["outdated"]
    assert [c["question"] for c in stored["flashcards"]] == ["What is the dew point?", "Define bypass factor"]
    assert files_needing_notes(conn) == []  # done until the file changes


def test_ollama_problems_explain_what_to_do(library):
    conn, rid = library
    with pytest.raises(NotesError, match="ollama pull"):
        make_notes_for(conn, rid, FakeOllama(models=[]).client())
    with pytest.raises(NotesError, match="Open the Ollama app"):
        Ollama(post=FakeOllama(down=True).post, get=FakeOllama().get).make_notes("t", "m", "text")
    assert FakeOllama(down=True).client().status() == {"running": False, "models": [], "model": None}


def test_model_choice():
    assert FakeOllama(models=["llama3.2:3b", "gemma3:4b"]).client().pick_model() == "gemma3:4b"
    assert FakeOllama(models=["llama3.2:3b"]).client().pick_model() == "llama3.2:3b"  # use what's installed
    assert FakeOllama(models=["llama3.2:3b"]).client(model="qwen3:8b").pick_model() is None
    assert FakeOllama(models=["qwen3:8b"]).client(model="qwen3").pick_model() == "qwen3:8b"


def test_claude_round_trip(library):
    conn, rid = library
    prompt = claude_prompt(conn, rid)
    assert "Psychrometry Lecture" in prompt and "FLASHCARDS:" in prompt and "bypass factor" in prompt
    reply = """Here you go!

**SUMMARY:**
Covers moist air properties
and cooling coil design.

**KEY POINTS:**
- Dew point is where condensation starts
2. Sensible heat ratio = sensible / total load

**FLASHCARDS:**
Q: What is the apparatus dew point?
A: The effective coil surface temperature.

Q: What does a bypass factor of 0.1 mean?
A: 10% of the air passes the coil
unaffected.
"""
    notes = parse_reply(reply)
    assert notes.summary == "Covers moist air properties and cooling coil design."
    assert notes.key_points == ["Dew point is where condensation starts", "Sensible heat ratio = sensible / total load"]
    assert notes.flashcards[1] == ("What does a bypass factor of 0.1 mean?", "10% of the air passes the coil unaffected.")
    assert save_notes(conn, rid, notes, TODAY) == 2 and get_notes(conn, rid)["source"] == "claude"
    with pytest.raises(NotesError, match="Paste Claude's whole reply"):
        parse_reply("Sorry, I can't help with that.")


def test_changed_file_marks_notes_outdated(library):
    conn, rid = library
    save_notes(conn, rid, Notes("S", [], [("Q", "A")], "claude"), TODAY)
    conn.execute("UPDATE resources SET content_hash = 'new' WHERE id = ?", (rid,))
    assert get_notes(conn, rid)["outdated"] and [r["id"] for r in files_needing_notes(conn)] == [rid]


# --- flashcard queue ------------------------------------------------------------------

def test_review_queue(library):
    conn, rid = library
    save_notes(conn, rid, Notes("S", [], [("Q1", "A1"), ("Q2", "A2")], "claude"), TODAY)
    queue = due_cards(conn, TODAY)
    assert [c["question"] for c in queue] == ["Q1", "Q2"] and queue[0]["module"] == "Architectural Engineering Design 3"
    record_review(conn, queue[0]["id"], "good", TODAY)
    record_review(conn, queue[1]["id"], "again", TODAY)
    assert [c["question"] for c in due_cards(conn, TODAY)] == ["Q2"] and due_count(conn, TODAY) == 1
    assert due_count(conn, TODAY + timedelta(days=1)) == 2
    assert card_totals(conn, TODAY) == [{"module_id": 1, "module": "Architectural Engineering Design 3", "total": 2,
                                         "due": 1, "learned": 0}]
    with pytest.raises(KeyError):
        record_review(conn, 999, "good", TODAY)
