"""Moodle file-URL helpers used by change detection."""

import pytest

from student_os.moodle.urls import cmid, comparable_file_url, file_revision, resource_key

PF = "https://moodle.nottingham.ac.uk/pluginfile.php"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{PF}/77/mod_resource/content/4/notes.pdf", 4),
        (f"{PF}/77/mod_folder/content/12/sub/dir/slides.pptx", 12),
        (f"{PF}/77/mod_page/content/3/figure.png", 3),
        (f"{PF}/77/mod_resource/content/4/notes.pdf?forcedownload=1", 4),
        (f"{PF}/9/course/section/1/brief.pdf", None),  # inline section file: no revision
        (f"{PF}/9/mod_label/intro/handout.pdf", None),
        ("https://moodle.nottingham.ac.uk/mod/resource/view.php?id=4", None),
        ("https://evil.example/pluginfile.php/77/mod_resource/content/4/x.pdf", None),
    ],
)
def test_file_revision(url, expected):
    assert file_revision(url) == expected


def test_comparable_file_url_drops_fragment_and_forcedownload_only():
    base = f"{PF}/77/mod_resource/content/4/notes.pdf"
    assert comparable_file_url(base + "?forcedownload=1#page=2") == base
    assert comparable_file_url(base + "?forcedownload=1&token=x") == base + "?token=x"
    assert comparable_file_url(base) == base


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://moodle.nottingham.ac.uk/mod/resource/view.php?id=4521", "cm:4521"),
        ("https://moodle.nottingham.ac.uk/mod/folder/view.php?id=88&forceview=1", "cm:88"),
        (f"{PF}/77/mod_folder/content/12/sub/Week%202.pptx", "file:77/mod_folder/content/sub/Week 2.pptx"),
        (f"{PF}/77/mod_folder/content/13/sub/Week%202.pptx?forcedownload=1",
         "file:77/mod_folder/content/sub/Week 2.pptx"),
        (f"{PF}/9/course/section/1/brief.pdf", "file:9/course/section/1/brief.pdf"),
        ("https://moodle.nottingham.ac.uk/course/view.php?id=3#section-2",
         "url:https://moodle.nottingham.ac.uk/course/view.php?id=3"),
    ],
)
def test_resource_key_ignores_revision(url, expected):
    assert resource_key(url) == expected


def test_cmid():
    assert cmid("https://moodle.nottingham.ac.uk/mod/assign/view.php?id=9") == 9
    assert cmid(f"{PF}/9/course/section/1/brief.pdf") is None
