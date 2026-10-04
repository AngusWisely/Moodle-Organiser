"""Dashboard: data for each view, and the local server's safety rules."""

import hashlib
import http.client
import json
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from student_os import db
from student_os.dashboard import queries
from student_os.dashboard.server import Dashboard
from student_os.library.index import index_library
from student_os.library.notes import Ollama
from tests.documents import make_docx, make_pdf

TODAY = date(2026, 10, 4)
DESIGN = "Architectural Engineering Design 3 (ABEE2014 UNUK AUT) (ABEE3039 UNUK AUT) (26-27)"


def stamp(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def project(tmp_path):
    conn = db.connect(tmp_path / "data" / "moodle.sqlite3")
    module = db.upsert_module(conn, moodle_id=1, name=DESIGN, url="https://moodle.nottingham.ac.uk/course/view.php?id=1",
                              at=stamp(30))
    db.select_modules(conn, [module.id])
    run = db.start_run(conn, "normal", stamp(0))
    folder = tmp_path / "materials" / "Design 3" / "Week 1"
    folder.mkdir(parents=True)

    def add(n, title, path=None, *, kind="resource", age=0, status=None):
        resource, _ = db.see_resource(conn, key=f"cm:{n}", module_id=module.id, section="Week 1", title=title,
                                      resource_type=kind, source_url=f"https://moodle.nottingham.ac.uk/mod/x/view.php?id={n}",
                                      at=stamp(age))
        conn.execute("UPDATE resources SET first_seen_at = ? WHERE id = ?", (stamp(age), resource.id))
        if path is not None:
            db.save_download(conn, resource.id, file_url=None, etag=None, last_modified=None,
                             local_path=path.relative_to(tmp_path).as_posix(), file_size=path.stat().st_size,
                             content_hash=hashlib.sha256(path.read_bytes()).hexdigest(), at=stamp(age), changed=True)
            db.log_event(conn, run, resource.id, "new", stamp(age))
        else:
            db.save_status(conn, resource.id, db.LINKED)
        if status:
            conn.execute("UPDATE resources SET status = ? WHERE id = ?", (status, resource.id))
        return resource.id

    ids = {
        "notes": add(1, "Psychrometry", make_pdf(folder / "psychro.pdf", [["Psychrometry", "Moist air"],
                                                                         ["Cooling coils", "Bypass factor"]])),
        "brief": add(2, "Coursework brief", make_docx(folder / "brief.docx", [("Submission", [
            "The report must be submitted by 14 November 2026.", "Reminder: submit by 14 November 2026.",
            "The 2025 cohort submitted by 3 March 2026."])]), age=10),
        "assign": add(3, "Coursework 1", kind="assign"),
        "folder": add(4, "Slides", kind="folder"),
        "old": add(5, "Old handout", make_pdf(folder / "old.pdf", [["Old", "Removed handout"]]), status=db.REMOVED),
    }
    conn.commit()
    index_library(conn, tmp_path, today=TODAY)
    yield conn, tmp_path, ids
    conn.close()


# --- data for each view --------------------------------------------------------------

def test_short_module_name():
    assert queries.short_module_name(DESIGN) == ("Architectural Engineering Design 3", "ABEE2014")
    assert queries.short_module_name("Year in Industry (Engineering) (UK) (27-28)") == ("Year in Industry", "")


def test_overview_lists_modules_deadlines_and_changes(project):
    conn, _, ids = project
    data = queries.overview(conn, TODAY)
    assert [(m["name"], m["code"], m["files"]) for m in data["modules"]] == [("Architectural Engineering Design 3", "ABEE2014", 2)]
    assert [(c["date"], c["kind"], c["title"]) for c in data["coming_up"]] == [("2026-11-14", "deadline", "Coursework brief")]
    assert {f["title"] for f in data["feed"]} == {"Psychrometry", "Coursework brief", "Old handout"}


def test_module_groups_items_and_hides_folders_and_removed(project):
    conn, _, ids = project
    module = queries.module_detail(conn, 1)
    items = module["sections"][0]["items"]
    assert [(i["title"], i["kind"], i["badge"]) for i in items] == [
        ("Psychrometry", "pdf", "new"), ("Coursework brief", "docx", ""), ("Coursework 1", "link", "")]
    assert queries.module_detail(conn, 999) is None


def test_file_detail_has_outline_and_dates(project):
    conn, _, ids = project
    file = queries.resource_detail(conn, ids["brief"], TODAY)
    assert file["outline"] == [{"page": 1, "title": "Submission"}]
    assert [(d["date"], d["kind"], d["upcoming"]) for d in file["dates"]] == [
        ("2026-03-03", "deadline", False), ("2026-11-14", "deadline", True), ("2026-11-14", "deadline", True)]
    assert file["has_file"] and file["kind"] == "docx"


def test_search_results_carry_page_and_kind(project):
    conn, _, ids = project
    [hit] = queries.search_results(conn, "bypass")
    assert (hit["title"], hit["page"], hit["page_title"], hit["kind"]) == ("Psychrometry", 2, "Cooling coils", "pdf")


# --- the server ---------------------------------------------------------------------

@pytest.fixture
def server(project):
    _, root, ids = project
    opened = []
    reply = {"summary": "Moist air basics.", "key_points": ["Dew point"],
             "flashcards": [{"question": "What is the dew point?", "answer": "Where condensation starts"}]}
    ollama = Ollama(get=lambda url, t: {"models": [{"name": "gemma3:4b"}]},
                    post=lambda url, payload, t: {"message": {"content": json.dumps(reply)}})
    app = Dashboard(root, token="secret-token", today=lambda: TODAY, opener=opened.append, ollama=ollama)
    httpd = app.make_server(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], ids, opened, root
    httpd.shutdown()
    httpd.server_close()


def request(port, path, *, method="GET", token="secret-token", host=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-Token"] = token
    payload = json.dumps(body).encode() if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, response.getheader("Content-Type"), body


def test_page_is_served_with_its_token(server):
    port, *_ = server
    status, content_type, body = request(port, "/", token=None)
    assert status == 200 and content_type.startswith("text/html") and b'content="secret-token"' in body


def test_api_needs_the_token(server):
    port, *_ = server
    assert request(port, "/api/overview", token=None)[0] == 403
    assert request(port, "/api/overview", token="wrong")[0] == 403
    status, _, body = request(port, "/api/overview")
    assert status == 200 and json.loads(body)["modules"][0]["code"] == "ABEE2014"


def test_other_hosts_are_refused(server):
    port, *_ = server
    assert request(port, "/", token=None, host="evil.example")[0] == 403  # DNS rebinding
    assert request(port, "/api/overview", host="evil.example")[0] == 403


def test_api_routes(server):
    port, ids, *_ = server
    assert json.loads(request(port, "/api/module/1")[2])["name"] == "Architectural Engineering Design 3"
    assert json.loads(request(port, f"/api/resource/{ids['notes']}")[2])["pages"] == 2
    assert json.loads(request(port, "/api/search?q=moist")[2])[0]["title"] == "Psychrometry"
    assert request(port, "/api/resource/999")[0] == 404
    assert request(port, "/api/nothing")[0] == 404


def test_files_served_only_from_known_paths(server, project):
    port, ids, _, root = server
    conn = project[0]
    status, content_type, body = request(port, f"/files/{ids['notes']}?t=secret-token", token=None)
    assert status == 200 and content_type == "application/pdf" and body.startswith(b"%PDF")
    assert request(port, f"/files/{ids['notes']}", token=None)[0] == 403
    assert request(port, f"/files/{ids['assign']}")[0] == 404  # a link has no file
    conn.execute("UPDATE resources SET local_path = '../../etc/passwd' WHERE id = ?", (ids["notes"],))
    conn.commit()
    assert request(port, f"/files/{ids['notes']}")[0] == 404


def test_open_in_default_app_is_post_only(server):
    port, ids, opened, root = server
    assert request(port, f"/api/open/{ids['brief']}")[0] == 404  # GET does nothing
    assert request(port, f"/api/open/{ids['brief']}", method="POST", token=None)[0] == 403
    status, _, body = request(port, f"/api/open/{ids['brief']}", method="POST")
    assert status == 200 and json.loads(body) == {"opened": True}
    assert [p.name for p in opened] == ["brief.docx"]


CLAUDE_REPLY = "SUMMARY:\nA coursework brief.\n\nKEY POINTS:\n- Due 14 November\n\nFLASHCARDS:\nQ: When is it due?\nA: 14 November 2026"


def test_ping_needs_no_token(server):
    port, *_ = server
    status, _, body = request(port, "/api/ping", token=None)
    assert status == 200 and json.loads(body) == {"app": "study-desk"}


def test_notes_from_this_mac(server, monkeypatch):
    port, ids, *_ = server
    import student_os.library.notes as notes_module
    monkeypatch.setattr(notes_module, "MAX_CHARS_LOCAL", 10_000)
    monkeypatch.setattr(notes_module, "document_text", lambda conn, rid, mx: ("words " * 60, False))
    status, _, body = request(port, f"/api/notes/{ids['notes']}/ollama", method="POST")
    assert status == 200 and json.loads(body)["summary"] == "Moist air basics."
    detail = json.loads(request(port, f"/api/resource/{ids['notes']}")[2])
    assert detail["notes"]["flashcards"][0]["question"] == "What is the dew point?"


def test_copy_for_claude_and_paste_back(server):
    port, ids, *_ = server
    prompt = json.loads(request(port, f"/api/claude-prompt/{ids['brief']}")[2])["prompt"]
    assert "FLASHCARDS:" in prompt and "14 November 2026" in prompt
    assert request(port, f"/api/notes/{ids['brief']}/claude", method="POST", body={"reply": "nonsense"})[0] == 400
    status, _, body = request(port, f"/api/notes/{ids['brief']}/claude", method="POST", body={"reply": CLAUDE_REPLY})
    assert status == 200 and json.loads(body)["source"] == "claude"
    assert request(port, f"/api/claude-prompt/{ids['assign']}")[0] == 400  # a link has no text


def test_revising_cards(server):
    port, ids, *_ = server
    request(port, f"/api/notes/{ids['brief']}/claude", method="POST", body={"reply": CLAUDE_REPLY})
    data = json.loads(request(port, "/api/cards")[2])
    [card] = data["cards"]
    assert card["question"] == "When is it due?" and data["totals"][0]["due"] == 1
    assert json.loads(request(port, "/api/overview")[2])["cards_due"] == 1
    status, _, body = request(port, f"/api/cards/{card['id']}/review", method="POST", body={"grade": "good"})
    assert status == 200 and json.loads(body)["due_on"] == "2026-10-05"
    assert json.loads(request(port, "/api/cards")[2])["cards"] == []
    assert request(port, f"/api/cards/{card['id']}/review", method="POST", body={"grade": "meh"})[0] == 400
    assert request(port, "/api/cards/999/review", method="POST", body={"grade": "good"})[0] == 404


def test_notes_for_all_files_in_background(server, monkeypatch):
    port, ids, *_ = server
    import time
    import student_os.library.notes as notes_module
    monkeypatch.setattr(notes_module, "document_text", lambda conn, rid, mx: ("words " * 60, False))
    monkeypatch.setattr(notes_module, "MIN_WORDS", 1)  # the fixture files are tiny
    before = json.loads(request(port, "/api/ai")[2])
    assert before["running"] and before["model"] == "gemma3:4b" and before["waiting"] == 2  # removed file excluded
    request(port, "/api/notes/all", method="POST")
    for _ in range(50):
        batch = json.loads(request(port, "/api/ai")[2])["batch"]
        if not batch["running"]:
            break
        time.sleep(0.05)
    assert (batch["total"], batch["done"], batch["errors"]) == (2, 2, [])
    assert json.loads(request(port, "/api/ai")[2])["waiting"] == 0
