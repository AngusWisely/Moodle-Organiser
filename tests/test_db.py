"""SQLite persistence: schema, module/resource records, history and index.csv migration."""

import csv
import hashlib
import sqlite3
from dataclasses import fields

import pytest

from student_os import db

T1, T2, T3 = "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z", "2026-10-03T08:00:00Z"
VIEW = "https://moodle.nottingham.ac.uk/mod/resource/view.php?id={}"
FILE_V4 = "https://moodle.nottingham.ac.uk/pluginfile.php/77/mod_resource/content/4/notes.pdf"


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "data" / "moodle.sqlite3")
    yield connection
    connection.close()


def add(conn, n: int, at: str = T1, module_id: int | None = None, **extra):
    if module_id is None:
        module_id = db.upsert_module(conn, moodle_id=1, name="Maths", url=None, at=at).id
    return db.see_resource(conn, key=f"cm:{n}", module_id=module_id, section="Week 1", title=f"Item {n}",
                           resource_type="resource", source_url=VIEW.format(n), at=at, **extra)


# --- schema ------------------------------------------------------------------------

def test_connect_creates_schema_and_is_reopenable(tmp_path):
    path = tmp_path / "data" / "moodle.sqlite3"
    db.connect(path).close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(resources)")]
    assert columns == [f.name for f in fields(db.Resource)]


def test_old_database_upgrades_without_losing_run_history(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(db._SCHEMA)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO sync_runs(started_at, finished_at, mode, outcome) VALUES(?,?,?,?)",
                 (T1, T2, "normal", "completed"))
    conn.commit()
    conn.close()
    upgraded = db.connect(path)
    row = upgraded.execute("SELECT outcome, bytes_received, selected_count, scanned_count FROM sync_runs").fetchone()
    assert tuple(row) == ("completed", 0, 0, 0)
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    upgraded.close()


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "x.sqlite3"
    conn = db.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError):
        db.connect(path)


# --- modules ------------------------------------------------------------------------

def test_module_matched_by_course_id_survives_rename(conn):
    first = db.upsert_module(conn, moodle_id=123, name="Design 3", url="u", at=T1)
    again = db.upsert_module(conn, moodle_id=123, name="Design 3 (2026-27)", url="u", at=T2)
    assert first.id == again.id and again.name == "Design 3 (2026-27)" and again.first_seen_at == T1


def test_imported_module_adopts_course_id_by_name(conn):
    imported = db.upsert_module(conn, moodle_id=None, name="Thermofluids", url=None, at=T1)
    synced = db.upsert_module(conn, moodle_id=456, name="Thermofluids", url="u", at=T2)
    assert synced.id == imported.id and synced.moodle_id == 456


def test_selected_modules_are_remembered(conn):
    a = db.upsert_module(conn, moodle_id=1, name="Maths", url=None, at=T1)
    b = db.upsert_module(conn, moodle_id=2, name="Acoustics", url=None, at=T1)
    db.select_modules(conn, [a.id, b.id])
    db.select_modules(conn, [b.id])
    assert [m.name for m in db.selected_modules(conn)] == ["Acoustics"]


# --- resources ----------------------------------------------------------------------

def test_new_resource_is_pending_with_cmid(conn):
    resource, reappeared = add(conn, 4521)
    assert (resource.status, resource.cmid, reappeared) == (db.PENDING, 4521, False)


def test_seeing_again_refreshes_metadata_not_history(conn):
    module_id = add(conn, 1)[0].module_id
    resource, _ = db.see_resource(conn, key="cm:1", module_id=module_id, section="Week 2", title="Renamed",
                                  resource_type="resource", source_url=VIEW.format(1), at=T2)
    assert (resource.title, resource.section, resource.first_seen_at, resource.last_seen_at) == \
        ("Renamed", "Week 2", T1, T2)


def test_download_then_unchanged_then_failure_keeps_file_record(conn):
    resource, _ = add(conn, 1)
    db.save_download(conn, resource.id, file_url=FILE_V4, etag='"e"', last_modified=None,
                     local_path="materials/Maths/notes.pdf", file_size=10, content_hash="a" * 64, at=T1,
                     changed=True)
    db.save_unchanged(conn, resource.id, file_url=FILE_V4.replace("/4/", "/5/"))
    db.save_failure(conn, resource.id, "timeout")
    stored = db.get_resource(conn, "cm:1")
    assert stored.status == db.DOWNLOADED and stored.content_hash == "a" * 64
    assert stored.local_path == "materials/Maths/notes.pdf" and stored.etag == '"e"'
    assert "/content/5/" in stored.file_url and stored.last_error == "timeout"
    assert stored.updated_at == T1


def test_identical_redownload_does_not_bump_updated_at(conn):
    resource, _ = add(conn, 1)
    kwargs = dict(file_url=FILE_V4, etag=None, last_modified=None, local_path="p", file_size=1,
                  content_hash="a" * 64)
    db.save_download(conn, resource.id, at=T1, changed=True, **kwargs)
    db.save_download(conn, resource.id, at=T2, changed=False, **kwargs)
    stored = db.get_resource(conn, "cm:1")
    assert (stored.updated_at, stored.downloaded_at, stored.last_error) == (T1, T2, None)


def test_link_status_never_hides_a_downloaded_file(conn):
    linked, _ = add(conn, 1)
    db.save_status(conn, linked.id, db.LINKED)
    kept, _ = add(conn, 2)
    db.save_download(conn, kept.id, file_url=None, etag=None, last_modified=None, local_path="p",
                     file_size=1, content_hash="a" * 64, at=T1, changed=True)
    db.save_status(conn, kept.id, db.VIDEO_LINK)
    assert db.get_resource(conn, "cm:1").status == db.LINKED
    assert db.get_resource(conn, "cm:2").status == db.DOWNLOADED


def test_removed_only_after_unseen_and_comes_back(conn):
    module_id = add(conn, 1, at=T1)[0].module_id
    add(conn, 2, at=T1, module_id=module_id)
    add(conn, 1, at=T2, module_id=module_id)  # run at T2 sees only item 1
    removed = db.mark_unseen_removed(conn, module_id, seen_since=T2)
    assert [r.key for r in removed] == ["cm:2"]
    assert db.mark_unseen_removed(conn, module_id, seen_since=T2) == []  # not removed twice
    back, reappeared = add(conn, 2, at=T3, module_id=module_id)
    assert reappeared and back.status == db.PENDING


# --- runs and events ----------------------------------------------------------------

def test_events_answer_whats_new_since(conn):
    resource, _ = add(conn, 1)
    run = db.start_run(conn, "normal", T1)
    db.log_event(conn, run, resource.id, "new", T1, new_hash="a" * 64)
    db.log_event(conn, run, resource.id, "updated", T3, old_hash="a" * 64, new_hash="b" * 64,
                 archived_path="materials/Maths/versions/x.pdf")
    db.finish_run(conn, run, "completed", T3)
    recent = db.events_since(conn, T2)
    assert [(e["kind"], e["module"], e["title"]) for e in recent] == [("updated", "Maths", "Item 1")]
    assert conn.execute("SELECT outcome FROM sync_runs").fetchone()[0] == "completed"


# --- index.csv migration -------------------------------------------------------------

def write_legacy(root, rows):
    index = root / "materials" / "index.csv"
    index.parent.mkdir(parents=True, exist_ok=True)
    with index.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=db.LEGACY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return index


def legacy_row(**values):
    row = dict.fromkeys(db.LEGACY_FIELDS, "")
    row.update(module="Maths", section="Week 1", title="T", type="resource")
    row.update(values)
    return row


def test_import_legacy_index_builds_baseline(conn, tmp_path):
    digest = hashlib.sha256(FILE_V4.encode()).hexdigest()[:8]
    notes = tmp_path / "materials" / "Maths" / "Week 1" / f"Notes-{digest}.pdf"
    notes.parent.mkdir(parents=True)
    notes.write_bytes(b"pdf bytes")
    (notes.parent / "Old copy-deadbeef.pdf").write_bytes(b"orphan")
    folder_file = "https://moodle.nottingham.ac.uk/pluginfile.php/80/mod_folder/content/2/w2.pptx"
    index = write_legacy(tmp_path, [
        legacy_row(title="Notes", url=VIEW.format(1), status="downloaded",
                   local_file=str(notes.relative_to(tmp_path))),
        legacy_row(title="Week 2", type="folder", url=folder_file, status="downloaded",
                   local_file="materials/Maths/Week 1/gone-12345678.pptx"),
        legacy_row(title="Recording.mp4", url=VIEW.format(3), status="linked video"),
        legacy_row(title="Coursework", type="assign", url="https://moodle.nottingham.ac.uk/mod/assign/view.php?id=4",
                   status="linked"),
        legacy_row(title="Broken", url=VIEW.format(5), status="error: TimeoutError"),
    ])

    report = db.import_legacy_index(conn, tmp_path, index, T1)

    assert (report.imported, report.hashed) == (5, 1)
    assert report.missing_files == ["materials/Maths/Week 1/gone-12345678.pptx"]
    assert report.unindexed_files == ["materials/Maths/Week 1/Old copy-deadbeef.pdf"]
    notes_row = db.get_resource(conn, "cm:1")
    assert notes_row.status == db.DOWNLOADED and notes_row.legacy_url_digest == digest
    assert notes_row.content_hash == hashlib.sha256(b"pdf bytes").hexdigest() and notes_row.file_size == 9
    folder_row = db.get_resource(conn, "file:80/mod_folder/content/w2.pptx")
    assert (folder_row.resource_type, folder_row.status, folder_row.file_url) == ("folder_file", db.PENDING,
                                                                                  folder_file)
    assert db.get_resource(conn, "cm:3").status == db.VIDEO_LINK
    assert db.get_resource(conn, "cm:4").status == db.LINKED
    broken = db.get_resource(conn, "cm:5")
    assert broken.status == db.PENDING and broken.last_error == "error: TimeoutError"
    assert conn.execute("SELECT COUNT(*) FROM sync_events").fetchone()[0] == 0  # baseline, not "new"


def test_import_is_safe_to_rerun_and_ignores_bad_paths(conn, tmp_path):
    index = write_legacy(tmp_path, [legacy_row(url=VIEW.format(1), status="downloaded",
                                               local_file="../../etc/passwd")])
    first = db.import_legacy_index(conn, tmp_path, index, T1)
    second = db.import_legacy_index(conn, tmp_path, index, T2)
    assert first.missing_files == ["../../etc/passwd"] and db.get_resource(conn, "cm:1").status == db.PENDING
    assert (second.imported, second.skipped_existing) == (0, 1)
    assert db.import_legacy_index(conn, tmp_path, tmp_path / "nope.csv", T1).imported == 0


def test_export_index_csv_round_trips_for_spreadsheets(conn, tmp_path):
    resource, _ = add(conn, 1)
    db.save_download(conn, resource.id, file_url=None, etag=None, last_modified=None,
                     local_path="materials/Maths/notes.pdf", file_size=1, content_hash="a" * 64, at=T1, changed=True)
    target = tmp_path / "materials" / "index.csv"
    assert db.export_index_csv(conn, target) == 1
    with target.open(newline="", encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle))
    assert (row["module"], row["status"], row["local_file"], row["updated_at"]) == \
        ("Maths", "downloaded", "materials/Maths/notes.pdf", T1)
