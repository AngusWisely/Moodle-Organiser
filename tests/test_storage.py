"""File storage: atomic writes, hashing, versioning and path safety."""

import hashlib
from datetime import datetime, timezone

import pytest

from student_os.moodle.detector import Decision, Outcome
from student_os.moodle.storage import (
    INCOMING,
    apply_decision,
    archive,
    clean_incoming,
    file_name_for_key,
    resolve_local,
    sha256_file,
    write_incoming,
)

WHEN = datetime(2026, 10, 4, 12, 30, 5, tzinfo=timezone.utc)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- hashing and incoming downloads ----------------------------------------

def test_write_incoming_hashes_while_writing(tmp_path):
    temp = write_incoming(tmp_path, b"lecture notes")
    assert temp.path.parent == tmp_path / INCOMING
    assert temp.path.read_bytes() == b"lecture notes"
    assert (temp.sha256, temp.size) == (sha(b"lecture notes"), 13)
    assert sha256_file(temp.path) == temp.sha256


def test_write_incoming_accepts_chunks(tmp_path):
    temp = write_incoming(tmp_path, [b"part one, ", b"part two"])
    assert (temp.sha256, temp.size) == (sha(b"part one, part two"), 18)


def test_failed_write_leaves_no_partial_file(tmp_path):
    def broken():
        yield b"half a file"
        raise ConnectionError("dropped")

    with pytest.raises(ConnectionError):
        write_incoming(tmp_path, broken())
    assert list((tmp_path / INCOMING).iterdir()) == []


def test_clean_incoming_removes_leftovers_from_crashed_runs(tmp_path):
    write_incoming(tmp_path, b"x")
    write_incoming(tmp_path, b"y")
    assert clean_incoming(tmp_path) == 2
    assert list((tmp_path / INCOMING).iterdir()) == []
    assert clean_incoming(tmp_path / "missing") == 0


# --- versioning --------------------------------------------------------------

def test_archive_moves_current_copy_into_versions(tmp_path):
    current = tmp_path / "Design 3" / "Week 1" / "Brief-1a2b3c4d.pdf"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"v1")
    archived = archive(current, WHEN)
    assert archived == current.parent / "versions" / "Brief-1a2b3c4d.20261004T123005Z.pdf"
    assert archived.read_bytes() == b"v1" and not current.exists()


def test_archive_never_overwrites_an_earlier_version(tmp_path):
    first = tmp_path / "notes.pdf"
    first.write_bytes(b"v1")
    a = archive(first, WHEN)
    first.write_bytes(b"v2")
    b = archive(first, WHEN)
    assert a != b and a.read_bytes() == b"v1" and b.read_bytes() == b"v2"


# --- applying a detector decision ------------------------------------------

def test_updated_file_archives_old_and_installs_new(tmp_path):
    target = tmp_path / "Maths" / "notes.pdf"
    target.parent.mkdir()
    target.write_bytes(b"old")
    temp = write_incoming(tmp_path, b"new")
    archived = apply_decision(Decision(Outcome.UPDATED, keep_new_file=True, archive_previous=True),
                              temp.path, target, WHEN)
    assert target.read_bytes() == b"new"
    assert archived is not None and archived.read_bytes() == b"old"
    assert not temp.path.exists()


def test_new_file_creates_folders(tmp_path):
    target = tmp_path / "Thermofluids" / "Week 2" / "sheet.pdf"
    temp = write_incoming(tmp_path, b"new")
    assert apply_decision(Decision(Outcome.NEW, keep_new_file=True), temp.path, target, WHEN) is None
    assert target.read_bytes() == b"new"


def test_unchanged_download_is_discarded_and_current_untouched(tmp_path):
    target = tmp_path / "notes.pdf"
    target.write_bytes(b"same")
    temp = write_incoming(tmp_path, b"same")
    assert apply_decision(Decision(Outcome.UNCHANGED), temp.path, target, WHEN) is None
    assert target.read_bytes() == b"same" and not temp.path.exists()
    assert not (tmp_path / "versions").exists()


def test_failed_decision_touches_nothing(tmp_path):
    target = tmp_path / "notes.pdf"
    target.write_bytes(b"keep me")
    apply_decision(Decision(Outcome.FAILED, reason="timeout"), None, target, WHEN)
    assert target.read_bytes() == b"keep me"


def test_keep_without_a_download_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        apply_decision(Decision(Outcome.NEW, keep_new_file=True), None, tmp_path / "x.pdf", WHEN)


# --- naming and path safety -------------------------------------------------

def test_new_names_derive_from_stable_key_not_revision():
    key = "cm:4521"
    v4 = file_name_for_key("Notes", key, "https://moodle.nottingham.ac.uk/pluginfile.php/7/mod_resource/content/4/n.pdf", "")
    v5 = file_name_for_key("Notes", key, "https://moodle.nottingham.ac.uk/pluginfile.php/7/mod_resource/content/5/n.pdf", "")
    assert v4 == v5 == f"Notes-{sha(key.encode())[:8]}.pdf"


def test_new_names_use_mime_type_when_url_has_no_extension():
    name = file_name_for_key("Slides", "cm:1", "https://moodle.nottingham.ac.uk/pluginfile.php/1/x/download",
                             "application/vnd.ms-powerpoint")
    assert name.endswith(".ppt")


def test_resolve_local_stays_inside_root(tmp_path):
    assert resolve_local(tmp_path, "materials/Maths/notes.pdf") == (tmp_path / "materials/Maths/notes.pdf").resolve()
    for bad in ("../outside.pdf", "materials/../../outside.pdf", "/etc/passwd"):
        with pytest.raises(ValueError):
            resolve_local(tmp_path, bad)
