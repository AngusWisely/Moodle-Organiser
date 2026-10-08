"""End-to-end sync against a fake Moodle: day-to-day runs, replacements, removals, failures, migration."""

import csv
import hashlib
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from student_os import db
from student_os.moodle.cli import choose_modules, course_id, format_module, format_totals, memory_copy
from student_os.moodle.detector import Outcome
from student_os.moodle.fetch import AuthExpired
from student_os.moodle.models import Item
from student_os.moodle.session import CourseScan
from student_os.moodle.sync import SyncOptions, Syncer
from tests.fakes import M, FakeMoodle, FakeResponse, html, pdf, redirect

COURSE = f"{M}/course/view.php?id=1"
LECTURE = f"{M}/mod/resource/view.php?id=10"
LECTURE_V4 = f"{M}/pluginfile.php/77/mod_resource/content/4/Lecture%201.pdf"
LECTURE_V5 = f"{M}/pluginfile.php/77/mod_resource/content/5/Lecture%201.pdf"
FOLDER = f"{M}/mod/folder/view.php?id=20"
SHEET_A = f"{M}/pluginfile.php/80/mod_folder/content/2/Sheet%20A.pdf"
SHEET_B = f"{M}/pluginfile.php/80/mod_folder/content/2/Sheet%20B.pdf"
COURSEWORK = f"{M}/mod/assign/view.php?id=30"
RECORDING = f"{M}/mod/resource/view.php?id=40"
RECORDING_FILE = f"{M}/pluginfile.php/90/mod_resource/content/1/week1.mp4"

ITEMS = [
    Item("Design 3", "Week 1", "Lecture 1", "resource", LECTURE),
    Item("Design 3", "Week 1", "Problem sheets", "folder", FOLDER),
    Item("Design 3", "Week 1", "Coursework 1", "assign", COURSEWORK),
    Item("Design 3", "Week 1", "Recording", "resource", RECORDING),
]


def moodle_routes() -> dict:
    return {
        LECTURE + "&redirect=1": redirect(LECTURE + "&redirect=1", LECTURE_V4),
        LECTURE_V4: pdf(LECTURE_V4, b"lecture v1", etag='"h1"'),
        FOLDER: html(FOLDER, f'<a href="{SHEET_A}">Sheet A.pdf</a><a href="{SHEET_B}">Sheet B.pdf</a>'),
        SHEET_A: pdf(SHEET_A, b"sheet a"),
        SHEET_B: pdf(SHEET_B, b"sheet b"),
        RECORDING + "&redirect=1": redirect(RECORDING + "&redirect=1", RECORDING_FILE),
    }


class Env:
    """A project folder, database and fake Moodle shared across several sync runs."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.materials = tmp_path / "materials"
        self.conn = db.connect(tmp_path / "data" / "moodle.sqlite3")
        self.moodle = FakeMoodle(moodle_routes())
        ticks = itertools.count()
        start = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
        self.clock = lambda: start + timedelta(seconds=next(ticks))
        with self.conn:
            self.module = db.upsert_module(self.conn, moodle_id=1, name="Design 3", url=COURSE,
                                           at="2026-10-01T07:00:00Z")

    def sync(self, items=ITEMS, *, complete=True, conn=None, **options):
        conn = conn or self.conn
        self.moodle.requests.clear()
        with conn:
            run_id = db.start_run(conn, "normal", "2026-10-01T08:00:00Z")
        syncer = Syncer(conn, self.moodle, root=self.root, materials=self.materials, run_id=run_id,
                        options=SyncOptions(**options), clock=self.clock)
        self.last_syncer = syncer
        return syncer.sync_module(self.module, CourseScan(list(items), complete, []))

    def resource(self, key):
        return db.get_resource(self.conn, key)

    def file(self, key):
        return self.root / self.resource(key).local_path

    def events(self):
        return [(e["kind"], e["title"]) for e in self.conn.execute(
            "SELECT e.kind, r.title FROM sync_events e JOIN resources r ON r.id = e.resource_id ORDER BY e.id")]


@pytest.fixture
def env(tmp_path):
    environment = Env(tmp_path)
    yield environment
    environment.conn.close()


SHEET_A_KEY = "file:80/mod_folder/content/Sheet A.pdf"


def test_first_sync_downloads_files_and_links_the_rest(env):
    report = env.sync()
    assert report.counts[Outcome.NEW] == 3 and report.counts[Outcome.LINKED] == 2 and not report.problems
    assert env.file("cm:10").read_bytes() == b"lecture v1"
    assert env.file("cm:10").parent == env.materials / "Design 3" / "Week 1"
    assert env.file(SHEET_A_KEY).read_bytes() == b"sheet a"
    assert env.resource("cm:30").status == db.LINKED
    assert env.resource("cm:40").status == db.VIDEO_LINK
    assert RECORDING_FILE not in env.moodle.urls()  # videos are never downloaded by default
    assert sorted(env.events()) == [("new", "Lecture 1"), ("new", "Sheet A.pdf"), ("new", "Sheet B.pdf")]
    assert not (env.materials / ".incoming").exists() or not any((env.materials / ".incoming").iterdir())


def test_quiet_day_downloads_nothing(env):
    env.sync()
    report = env.sync()
    assert report.counts[Outcome.UNCHANGED] == 3 and report.counts[Outcome.NEW] == 0
    assert set(env.moodle.urls()) == {LECTURE + "&redirect=1", FOLDER, RECORDING + "&redirect=1"}
    assert len(env.events()) == 3  # nothing new logged


def test_downloaded_bytes_count_actual_file_bodies(env):
    env.sync()
    assert env.last_syncer.bytes_received == len(b"lecture v1") + len(b"sheet a") + len(b"sheet b")
    env.sync()
    assert env.last_syncer.bytes_received == 0


def test_lecturer_replaces_file_at_same_link(env):
    env.sync()
    path = env.file("cm:10")
    env.moodle.routes[LECTURE + "&redirect=1"] = redirect(LECTURE + "&redirect=1", LECTURE_V5)
    env.moodle.routes[LECTURE_V5] = pdf(LECTURE_V5, b"lecture v2 with corrections")
    report = env.sync()
    assert report.counts[Outcome.UPDATED] == 1
    assert env.file("cm:10") == path and path.read_bytes() == b"lecture v2 with corrections"
    versions = list((path.parent / "versions").iterdir())
    assert [v.read_bytes() for v in versions] == [b"lecture v1"]
    stored = env.resource("cm:10")
    assert stored.file_url == LECTURE_V5 and stored.content_hash == hashlib.sha256(b"lecture v2 with corrections").hexdigest()
    event = env.conn.execute("SELECT * FROM sync_events WHERE kind = 'updated'").fetchone()
    assert event["archived_path"].startswith("materials/Design 3/Week 1/versions/")


def test_revision_bump_with_identical_content_is_unchanged(env):
    env.sync()
    env.moodle.routes[LECTURE + "&redirect=1"] = redirect(LECTURE + "&redirect=1", LECTURE_V5)
    env.moodle.routes[LECTURE_V5] = pdf(LECTURE_V5, b"lecture v1")
    report = env.sync()
    assert report.counts[Outcome.UPDATED] == 0 and report.counts[Outcome.UNCHANGED] == 3
    assert not (env.file("cm:10").parent / "versions").exists()
    assert env.resource("cm:10").file_url == LECTURE_V5  # next time this revision is trusted
    env.sync()
    assert LECTURE_V5 not in env.moodle.urls()


def test_conditional_request_when_server_gave_an_etag(env):
    env.sync()
    env.moodle.routes[LECTURE + "&redirect=1"] = redirect(LECTURE + "&redirect=1", LECTURE_V5)

    def not_modified(url, headers):
        assert headers.get("If-None-Match") == '"h1"'
        return FakeResponse(304, url, {"etag": '"h1"'})

    env.moodle.routes[LECTURE_V5] = not_modified
    report = env.sync()
    assert report.counts[Outcome.UNCHANGED] == 3 and report.counts[Outcome.UPDATED] == 0


def test_removed_only_after_a_complete_scan_and_files_kept(env):
    env.sync()
    without_coursework = [i for i in ITEMS if i.type != "assign"]
    assert env.sync(without_coursework, complete=False).removed == 0
    assert env.resource("cm:30").status == db.LINKED
    report = env.sync(without_coursework)
    assert report.removed == 1 and env.resource("cm:30").status == db.REMOVED
    assert ("removed", "Coursework 1") in env.events()
    back = env.sync()
    assert env.resource("cm:30").status == db.LINKED and back.removed == 0
    assert ("reappeared", "Coursework 1") in env.events()


def test_folder_failure_blocks_removal_of_its_files(env):
    env.sync()
    env.moodle.routes[FOLDER] = FakeResponse(500, FOLDER)
    report = env.sync()
    assert report.removed == 0 and env.resource(SHEET_A_KEY).status == db.DOWNLOADED
    assert any("folder unavailable" in p for p in report.problems)


def test_one_failure_does_not_stop_the_rest_and_is_retried(env):
    env.moodle.routes[SHEET_B] = FakeResponse(500, SHEET_B)
    report = env.sync()
    assert report.counts[Outcome.NEW] == 2 and report.counts[Outcome.FAILED] == 1
    failed = env.resource("file:80/mod_folder/content/Sheet B.pdf")
    assert failed.status == db.PENDING and failed.last_error == "HTTP 500"
    env.moodle.routes[SHEET_B] = pdf(SHEET_B, b"sheet b")
    assert env.sync().counts[Outcome.NEW] == 1


def test_failure_never_damages_a_good_copy(env):
    env.sync()
    env.moodle.routes[LECTURE_V4] = FakeResponse(500, LECTURE_V4)
    report = env.sync(refresh=True)
    assert report.counts[Outcome.FAILED] == 1
    assert env.file("cm:10").read_bytes() == b"lecture v1"
    assert env.resource("cm:10").status == db.DOWNLOADED


def test_expired_session_stops_the_run_safely(env):
    env.sync()
    env.moodle.routes[LECTURE_V4] = redirect(LECTURE_V4, f"{M}/login/index.php")
    with pytest.raises(AuthExpired):
        env.sync(refresh=True)
    assert env.file("cm:10").read_bytes() == b"lecture v1"
    assert env.resource("cm:10").status == db.DOWNLOADED and env.resource("cm:30").status == db.LINKED


def test_deleted_local_file_is_restored(env):
    env.sync()
    env.file("cm:10").unlink()
    report = env.sync()
    assert report.counts[Outcome.RESTORED] == 1 and env.file("cm:10").read_bytes() == b"lecture v1"


def test_refresh_redownloads_but_only_versions_real_changes(env):
    env.sync()
    report = env.sync(refresh=True)
    assert report.counts[Outcome.UNCHANGED] == 3
    assert LECTURE_V4 in env.moodle.urls() and RECORDING_FILE not in env.moodle.urls()
    assert not (env.file("cm:10").parent / "versions").exists()


def test_videos_downloaded_with_option(env):
    env.moodle.routes[RECORDING_FILE] = FakeResponse(200, RECORDING_FILE, {"content-type": "video/mp4"}, b"mp4")
    report = env.sync(videos=True)
    assert report.counts[Outcome.NEW] == 4 and env.file("cm:40").suffix == ".mp4"


def test_resource_served_directly_is_not_fetched_twice(env):
    probe = LECTURE + "&redirect=1"
    env.moodle.routes[probe] = pdf(probe, b"served inline")
    report = env.sync(ITEMS[:1])
    assert report.counts[Outcome.NEW] == 1 and env.moodle.urls() == [probe]
    assert env.file("cm:10").read_bytes() == b"served inline"


def test_legacy_downloads_are_recognised_without_downloading(env):
    digest = hashlib.sha256(LECTURE_V4.encode()).hexdigest()[:8]
    legacy = env.materials / "Design 3" / "Week 1" / f"Lecture 1-{digest}.pdf"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"lecture v1")
    index = env.materials / "index.csv"
    with index.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=db.LEGACY_FIELDS)
        writer.writeheader()
        writer.writerow({"module": "Design 3", "section": "Week 1", "title": "Lecture 1", "type": "resource",
                         "url": LECTURE, "status": "downloaded", "local_file": str(legacy.relative_to(env.root))})
    db.import_legacy_index(env.conn, env.root, index, "2026-09-30T00:00:00Z")
    report = env.sync(ITEMS[:1])
    assert report.counts[Outcome.UNCHANGED] == 1 and LECTURE_V4 not in env.moodle.urls()
    assert env.resource("cm:10").file_url == LECTURE_V4 and env.file("cm:10") == legacy


def test_dry_run_changes_nothing(env):
    env.sync(ITEMS[:1])
    before = env.conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    files_before = sorted(p for p in env.materials.rglob("*") if p.is_file())
    copy = memory_copy(env.conn)
    report = env.sync(conn=copy, dry_run=True)
    assert report.to_fetch == 2 and report.counts[Outcome.UNCHANGED] == 1  # two new sheets, lecture unchanged
    assert SHEET_A not in env.moodle.urls()
    assert env.conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == before
    assert sorted(p for p in env.materials.rglob("*") if p.is_file()) == files_before


# --- CLI helpers ------------------------------------------------------------------

def test_report_format_matches_spec(env):
    report = env.sync()
    text = format_module(report)
    assert text.splitlines()[:2] == ["✓ 5 checked", "+ 3 new"]
    totals = format_totals([report])
    assert "New: 3" in totals and "Updated: 0" in totals and "Errors: 0" in totals


def test_course_id():
    assert course_id(f"{M}/course/view.php?id=123") == 123
    assert course_id(f"{M}/course/view.php") is None


def test_module_choice_is_remembered(env, monkeypatch):
    with env.conn:
        other = db.upsert_module(env.conn, moodle_id=2, name="Maths", url=f"{M}/course/view.php?id=2",
                                 at="2026-10-01T07:00:00Z")
    answers = iter(["9", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    on_moodle = [env.module, other]
    assert [m.name for m in choose_modules(env.conn, on_moodle, force=False)] == ["Maths"]
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("should not ask again"))
    assert [m.name for m in choose_modules(env.conn, on_moodle, force=False)] == ["Maths"]


def test_recent_changes_grouped_by_module_in_local_time(env):
    from datetime import timezone as tz_module
    from student_os.moodle.cli import format_recent

    env.sync(ITEMS[:1])
    events = db.events_since(env.conn, "2026-01-01T00:00:00Z")
    text = format_recent(events, 1, tz=tz_module(timedelta(hours=1)))
    assert text.splitlines() == ["Changes in the last 1 day(s)", "", "Design 3",
                                 "  01 Oct 09:00  new        Week 1 / Lecture 1"]
    assert format_recent([], 2) == "No changes in the last 2 day(s)."
