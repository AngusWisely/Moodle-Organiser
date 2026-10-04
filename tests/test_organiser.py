"""The legacy organiser.py entry point still works and still exposes its old names."""

import organiser
from organiser import Item, process


def test_new_video_is_indexed_without_request():
    item = Item("Module", "Week 1", "Recording.mp4", "resource", "https://moodle.nottingham.ac.uk/mod/resource/view.php?id=4")
    result = process(None, item, {}, videos=False, refresh=False)
    assert result[0].status == "linked video"


def test_non_file_activity_passes_through_without_request():
    item = Item("Module", "Week 1", "Coursework", "assign", "https://moodle.nottingham.ac.uk/mod/assign/view.php?id=9")
    assert process(None, item, {}, videos=False, refresh=False) == [item]


def test_cached_download_is_reused_without_request(tmp_path, monkeypatch):
    monkeypatch.setattr(organiser, "ROOT", tmp_path)
    (tmp_path / "notes.pdf").write_bytes(b"pdf")
    url = "https://moodle.nottingham.ac.uk/mod/resource/view.php?id=4"
    cached = Item("Module", "Week 1", "Notes", "resource", url, "downloaded", "notes.pdf")
    item = Item("Module", "Week 1", "Notes", "resource", url)
    assert process(None, item, {("Module", url): cached}, videos=False, refresh=False) == [cached]


def test_old_import_names_still_available():
    for name in ("activities", "allowed", "course_links", "file_name", "files_from_html", "safe_name", "section_links"):
        assert callable(getattr(organiser, name))
