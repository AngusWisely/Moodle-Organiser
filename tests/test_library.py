"""Course library: text extraction, outlines, dates and full-text search."""

import hashlib
from datetime import date

import pytest

from student_os import db
from student_os.library.dates import find_dates
from student_os.library.extract import UnsupportedFile, extract
from student_os.library.index import dates_between, fts_query, index_library, outline, search
from tests.documents import make_docx, make_pdf, make_pptx

TODAY = date(2026, 10, 4)


# --- extraction ----------------------------------------------------------------------

def test_pdf_pages_and_titles_skip_page_numbers(tmp_path):
    doc = extract(make_pdf(tmp_path / "a.pdf", [["Psychrometry", "Moist air basics"],
                                                ["12", "Mixing processes", "Adiabatic mixing"]]))
    assert doc.kind == "pdf" and len(doc.pages) == 2
    assert doc.outline() == [(1, "Psychrometry"), (2, "Mixing processes")]
    assert "Adiabatic mixing" in doc.text and doc.word_count > 5


def test_pptx_titles_body_and_speaker_notes(tmp_path):
    doc = extract(make_pptx(tmp_path / "b.pptx", [("Duct Sizing", "Equal friction"), ("Duct Sizing", "Continued"),
                                                 ("Fans", "Fan laws")], notes={2: "Mention fan curves"}))
    assert doc.outline() == [(1, "Duct Sizing"), (3, "Fans")]  # repeated titles collapse
    assert "Mention fan curves" in doc.pages[2].text


def test_docx_sections_follow_headings(tmp_path):
    doc = extract(make_docx(tmp_path / "c.docx", [("Brief", ["Design a ventilation system."]),
                                                  ("Submission", ["Submit by Friday 14th November."])]))
    assert doc.outline() == [(1, "Brief"), (2, "Submission")]


def test_unsupported_types(tmp_path):
    (tmp_path / "old.ppt").write_bytes(b"binary")
    with pytest.raises(UnsupportedFile):
        extract(tmp_path / "old.ppt")


# --- dates -----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Coursework must be submitted by Friday 14th November at 4pm.", [(date(2026, 11, 14), "deadline")]),
        ("Deadline: November 28, 2026", [(date(2026, 11, 28), "deadline")]),
        ("Exam: 12/01/2027.", [(date(2027, 1, 12), "exam")]),
        ("Class test on 2026-12-03.", [(date(2026, 12, 3), "exam")]),
        ("Lecture on 3 Oct.", [(date(2026, 10, 3), "date")]),
        ("Hand-in 5th Feb.", [(date(2027, 2, 5), "deadline")]),      # no year: next February
        ("Ratio 3/4 and figure 12.5 are not dates.", []),
        ("Published 31/02/2026.", []),                                   # impossible date
    ],
)
def test_find_dates(text, expected):
    assert [(m.on, m.kind) for m in find_dates(text, today=TODAY)] == expected


def test_deadline_wording_is_judged_per_sentence():
    found = find_dates("Coursework must be submitted by 14 November. Lecture on 3 Oct.", today=TODAY)
    assert [(m.on.day, m.kind, m.snippet) for m in found] == [
        (14, "deadline", "Coursework must be submitted by 14 November."), (3, "date", "Lecture on 3 Oct.")]


# --- index and search -------------------------------------------------------------------

@pytest.fixture
def library(tmp_path):
    conn = db.connect(tmp_path / "data" / "moodle.sqlite3")
    module = db.upsert_module(conn, moodle_id=1, name="Design 3", url=None, at="2026-10-01T00:00:00Z")
    folder = tmp_path / "materials" / "Design 3" / "Week 1"
    folder.mkdir(parents=True)

    def add(n: int, path, status=db.DOWNLOADED):
        resource, _ = db.see_resource(conn, key=f"cm:{n}", module_id=module.id, section="Week 1",
                                      title=path.stem, resource_type="resource",
                                      source_url=f"https://moodle.nottingham.ac.uk/mod/resource/view.php?id={n}",
                                      at="2026-10-01T00:00:00Z")
        db.save_download(conn, resource.id, file_url=None, etag=None, last_modified=None,
                         local_path=path.relative_to(tmp_path).as_posix(), file_size=path.stat().st_size,
                         content_hash=hashlib.sha256(path.read_bytes()).hexdigest(), at="2026-10-01T00:00:00Z",
                         changed=True)
        if status != db.DOWNLOADED:
            conn.execute("UPDATE resources SET status = ? WHERE id = ?", (status, resource.id))
        conn.commit()
        return resource.id

    ids = {
        "pdf": add(1, make_pdf(folder / "Psychrometry.pdf", [["Psychrometry", "Moist air and dew point"],
                                                            ["Mixing", "Adiabatic mixing of airstreams"]])),
        "pptx": add(2, make_pptx(folder / "Ducts.pptx", [("Duct Sizing", "Equal friction method")])),
        "docx": add(3, make_docx(folder / "Brief.docx", [("Submission",
                                                          ["Coursework must be submitted by 14 November."])])),
        "video": add(4, _write(folder / "Recording.mp4", b"mp4")),
    }
    yield conn, tmp_path, ids
    conn.close()


def _write(path, data):
    path.write_bytes(data)
    return path


def test_index_reads_each_file_once(library):
    conn, root, ids = library
    report = index_library(conn, root, today=TODAY)
    assert (report.indexed, report.unsupported, report.failed) == (3, 1, [])
    again = index_library(conn, root, today=TODAY)
    assert (again.indexed, again.unchanged) == (0, 4)
    assert outline(conn, ids["pdf"]) == [(1, "Psychrometry"), (2, "Mixing")]


def test_changed_file_is_reindexed_and_old_text_forgotten(library):
    conn, root, ids = library
    index_library(conn, root, today=TODAY)
    path = root / db.get_resource(conn, "cm:2").local_path
    make_pptx(path, [("Fans", "Fan laws and system curves")])
    conn.execute("UPDATE resources SET content_hash = ? WHERE id = ?",
                 (hashlib.sha256(path.read_bytes()).hexdigest(), ids["pptx"]))
    assert index_library(conn, root, today=TODAY).indexed == 1
    assert search(conn, "friction") == [] and search(conn, "fan laws")[0].title == "Ducts"


def test_search_finds_words_with_snippets_and_prefixes(library):
    conn, root, ids = library
    index_library(conn, root, today=TODAY)
    hits = search(conn, "adiabatic mix")  # last word is a prefix
    assert [(h.title, h.page, h.page_title) for h in hits] == [("Psychrometry", 2, "Mixing")]
    assert "[[Adiabatic]]" in hits[0].snippet and hits[0].module == "Design 3"
    assert search(conn, 'dew "point') and search(conn, "   ") == []  # stray quotes are harmless


def test_dates_listed_with_their_file(library):
    conn, root, ids = library
    index_library(conn, root, today=TODAY)
    rows = dates_between(conn, TODAY)
    assert [(r["on_date"], r["kind"], r["title"]) for r in rows] == [("2026-11-14", "deadline", "Brief")]
    assert dates_between(conn, date(2026, 12, 1)) == []


def test_removed_files_stay_searchable_but_leave_the_timeline(library):
    conn, root, ids = library
    conn.execute("UPDATE resources SET status = ? WHERE id = ?", (db.REMOVED, ids["docx"]))
    index_library(conn, root, today=TODAY)
    assert search(conn, "coursework") and dates_between(conn, TODAY) == []


def test_damaged_file_is_reported_not_fatal(library):
    conn, root, ids = library
    (root / db.get_resource(conn, "cm:1").local_path).write_bytes(b"%PDF-1.4 broken")
    conn.execute("UPDATE resources SET content_hash = 'x' WHERE id = ?", (ids["pdf"],))
    report = index_library(conn, root, today=TODAY)
    assert report.indexed == 2 and len(report.failed) == 1 and "Psychrometry" in report.failed[0]


def test_fts_query_is_safe():
    assert fts_query('dew "point') == '"dew" "point"*'
    assert fts_query("CIBSE Guide-A") == '"CIBSE" "Guide" "A"*'
    assert fts_query("?!") == ""
