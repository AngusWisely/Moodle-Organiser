"""Change detection: what to fetch (plan) and what the fetch meant (classify).

Each case is one row of the decision table in the Milestone 1 plan.
"""

import hashlib

import pytest

from student_os.moodle.detector import (
    Decision,
    Failed,
    Fetched,
    Known,
    LocalFile,
    NotModified,
    Outcome,
    Plan,
    Remote,
    classify,
    plan,
)

FILE_V4 = "https://moodle.nottingham.ac.uk/pluginfile.php/77/mod_resource/content/4/notes.pdf"
FILE_V5 = "https://moodle.nottingham.ac.uk/pluginfile.php/77/mod_resource/content/5/notes.pdf"
INLINE = "https://moodle.nottingham.ac.uk/pluginfile.php/9/course/section/1/brief.pdf"
VIDEO = "https://moodle.nottingham.ac.uk/pluginfile.php/77/mod_resource/content/2/week1.mp4"

HASH_A = "a" * 64
HASH_B = "b" * 64

INTACT = LocalFile(exists=True, size=100)
MISSING = LocalFile(exists=False)
TRUNCATED = LocalFile(exists=True, size=40)


def known(**overrides) -> Known:
    values = {"content_hash": HASH_A, "file_size": 100, "file_url": FILE_V4}
    values.update(overrides)
    return Known(**values)


# --- plan: decide what (if anything) to request -----------------------------

@pytest.mark.parametrize(
    ("stored", "local", "remote", "flags", "expected"),
    [
        pytest.param(None, MISSING, Remote(FILE_V4), {}, Plan.DOWNLOAD, id="never seen"),
        pytest.param(known(content_hash=None, file_size=None), MISSING, Remote(FILE_V4), {}, Plan.DOWNLOAD,
                     id="seen but never downloaded (earlier failure)"),
        pytest.param(known(), INTACT, Remote(FILE_V4), {}, Plan.SKIP, id="same revision, local intact"),
        pytest.param(known(), INTACT, Remote(FILE_V4 + "?forcedownload=1"), {}, Plan.SKIP,
                     id="same revision, forcedownload param ignored"),
        pytest.param(known(), MISSING, Remote(FILE_V4), {}, Plan.DOWNLOAD, id="local file deleted"),
        pytest.param(known(), TRUNCATED, Remote(FILE_V4), {}, Plan.DOWNLOAD, id="local file truncated"),
        pytest.param(known(), INTACT, Remote(FILE_V4), {"refresh": True}, Plan.DOWNLOAD, id="force refresh"),
        pytest.param(known(), INTACT, Remote(FILE_V5), {}, Plan.DOWNLOAD,
                     id="revision changed, no validators"),
        pytest.param(known(etag='"abc"'), INTACT, Remote(FILE_V5), {}, Plan.CONDITIONAL_GET,
                     id="revision changed, etag known"),
        pytest.param(known(last_modified="Sun, 04 Oct 2026 09:00:00 GMT"), INTACT, Remote(FILE_V5), {},
                     Plan.CONDITIONAL_GET, id="revision changed, last-modified known"),
        pytest.param(known(file_url=INLINE, etag='"abc"'), INTACT, Remote(INLINE), {}, Plan.CONDITIONAL_GET,
                     id="unversioned URL with validators: cheap check every run"),
        pytest.param(known(file_url=INLINE), INTACT, Remote(INLINE), {}, Plan.SKIP,
                     id="unversioned URL without validators: left to --refresh"),
        pytest.param(known(), INTACT, Remote(None), {}, Plan.DOWNLOAD, id="file URL unknown before fetching"),
        pytest.param(None, MISSING, Remote(VIDEO, is_video=True), {}, Plan.LINK_VIDEO, id="new video, default"),
        pytest.param(None, MISSING, Remote(VIDEO, is_video=True), {"videos": True}, Plan.DOWNLOAD,
                     id="new video with --videos"),
        pytest.param(known(file_url=VIDEO), INTACT, Remote(VIDEO, is_video=True), {"refresh": True}, Plan.SKIP,
                     id="downloaded video kept without --videos, even on refresh"),
    ],
)
def test_plan(stored, local, remote, flags, expected):
    assert plan(stored, local, remote, **flags) is expected


def test_plan_recognises_file_downloaded_by_legacy_organiser():
    # organiser.py named files <title>-<sha256(resolved url)[:8]><ext>; the digest
    # proves the local copy came from this exact revision, so no download is needed.
    digest = hashlib.sha256(FILE_V4.encode()).hexdigest()[:8]
    migrated = known(file_url=None, legacy_url_digest=digest)
    assert plan(migrated, INTACT, Remote(FILE_V4)) is Plan.SKIP
    assert plan(migrated, INTACT, Remote(FILE_V5)) is Plan.DOWNLOAD


# --- classify: interpret the fetch -----------------------------------------

@pytest.mark.parametrize(
    ("stored", "local", "result", "expected"),
    [
        pytest.param(None, MISSING, Fetched(HASH_A, 100), Decision(Outcome.NEW, keep_new_file=True), id="new"),
        pytest.param(known(content_hash=None, file_size=None), MISSING, Fetched(HASH_A, 100),
                     Decision(Outcome.NEW, keep_new_file=True), id="first success after earlier failure"),
        pytest.param(known(), INTACT, Fetched(HASH_A, 100), Decision(Outcome.UNCHANGED),
                     id="re-uploaded identical file: discard download"),
        pytest.param(known(), INTACT, Fetched(HASH_B, 120),
                     Decision(Outcome.UPDATED, keep_new_file=True, archive_previous=True), id="genuinely replaced"),
        pytest.param(known(), MISSING, Fetched(HASH_A, 100), Decision(Outcome.RESTORED, keep_new_file=True),
                     id="deleted locally, same remote content"),
        pytest.param(known(), MISSING, Fetched(HASH_B, 120), Decision(Outcome.UPDATED, keep_new_file=True),
                     id="deleted locally and replaced remotely: nothing to archive"),
        pytest.param(known(), TRUNCATED, Fetched(HASH_A, 100),
                     Decision(Outcome.RESTORED, keep_new_file=True, archive_previous=True),
                     id="truncated local copy repaired, damaged copy kept aside"),
        pytest.param(known(), INTACT, NotModified(), Decision(Outcome.UNCHANGED), id="304 not modified"),
        pytest.param(known(), INTACT, Failed("timeout"), Decision(Outcome.FAILED, reason="timeout"),
                     id="failure preserves state"),
        pytest.param(None, MISSING, NotModified(), Decision(Outcome.FAILED, reason="304 without a stored copy"),
                     id="impossible 304 treated as failure"),
    ],
)
def test_classify(stored, local, result, expected):
    assert classify(stored, local, result) == expected


def test_failure_never_keeps_or_archives_files():
    decision = classify(known(), INTACT, Failed("login page returned", auth=True))
    assert decision.outcome is Outcome.FAILED
    assert not decision.keep_new_file and not decision.archive_previous
    assert decision.auth_failure


# --- local file state --------------------------------------------------------

def test_local_file_from_path(tmp_path):
    target = tmp_path / "notes.pdf"
    assert LocalFile.from_path(target) == LocalFile(exists=False)
    assert LocalFile.from_path(None) == LocalFile(exists=False)
    target.write_bytes(b"12345")
    assert LocalFile.from_path(target) == LocalFile(exists=True, size=5)
    assert LocalFile.from_path(tmp_path) == LocalFile(exists=False)  # a directory is not a file
