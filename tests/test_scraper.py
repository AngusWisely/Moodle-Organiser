"""Parsing, URL safety and naming. Pins current behaviour before the sync refactor."""

import hashlib

from student_os.moodle.scraper import activities, course_links, files_from_html, section_links
from student_os.moodle.storage import file_name, safe_name
from student_os.moodle.urls import allowed

COURSE = "https://moodle.nottingham.ac.uk/course/view.php?id=1"


def test_course_links():
    html = '<a href="/course/view.php?id=123">Acoustics</a><a href="/course/view.php?id=123">Acoustics</a>'
    assert course_links(html) == [("Acoustics", "https://moodle.nottingham.ac.uk/course/view.php?id=123")]


def test_course_links_skips_generic_labels_and_other_hosts():
    html = (
        '<a href="/course/view.php?id=1">View course</a>'
        '<a href="https://evil.example/course/view.php?id=2">Phish</a>'
        '<a href="/course/view.php?id=3#section-2">Maths</a>'
    )
    assert course_links(html) == [("Maths", "https://moodle.nottingham.ac.uk/course/view.php?id=3")]


def test_section_and_activity():
    html = '''<li class="section"><h3 class="sectionname">Lighting</h3>
        <li class="activity"><a href="/mod/resource/view.php?id=2"><span class="instancename">Lecture Notes</span></a></li>
        </li>'''
    found = activities(html, "Acoustics", COURSE)
    assert [(x.section, x.title, x.type) for x in found] == [("Lighting", "Lecture Notes", "resource")]


def test_activity_kinds_and_file_suffix_stripped():
    html = '''<li class="section"><h3 class="sectionname">Week 1</h3>
        <li class="activity"><a href="/mod/folder/view.php?id=5"><span class="instancename">Slides</span></a></li>
        <li class="activity"><a href="/mod/assign/view.php?id=6"><span class="instancename">Coursework 1</span></a></li>
        <li class="activity"><a href="/pluginfile.php/9/course/section/1/brief.pdf">Brief File</a></li>
        <li class="activity"><a href="/user/profile.php?id=7">Lecturer</a></li>
        </li>'''
    found = activities(html, "Design 3", COURSE)
    assert [(x.title, x.type) for x in found] == [("Slides", "folder"), ("Coursework 1", "assign"), ("Brief", "file")]


def test_activities_without_sections_default_to_general():
    html = '<a href="/mod/url/view.php?id=8">Reading list</a>'
    assert [(x.section, x.type) for x in activities(html, "Maths", COURSE)] == [("General", "url")]


def test_section_links_dedupe_and_safety():
    html = (
        '<a href="/course/section.php?id=10">A</a><a href="/course/section.php?id=10">A</a>'
        '<a href="http://moodle.nottingham.ac.uk/course/section.php?id=11">insecure</a>'
    )
    assert section_links(html, COURSE) == ["https://moodle.nottingham.ac.uk/course/section.php?id=10"]


def test_file_links_and_safety():
    html = '<a href="/pluginfile.php/1/mod_folder/content/0/intro.pdf">Intro</a>'
    assert len(files_from_html(html, "https://moodle.nottingham.ac.uk/mod/folder/view.php?id=1")) == 1
    assert not allowed("https://moodle.nottingham.ac.uk.evil.example/pluginfile.php/1")
    assert safe_name("../bad:course") == "bad-course"
    assert file_name("Lecture", "https://moodle.nottingham.ac.uk/pluginfile.php/1/lecture.pdf", "application/pdf").endswith(".pdf")


def test_files_from_html_uses_filename_when_link_has_no_text():
    html = '<a href="/pluginfile.php/1/mod_folder/content/3/Week%202.pptx"></a><a href="https://other.example/pluginfile.php/1/x.pdf">x</a>'
    assert files_from_html(html, COURSE) == [
        ("https://moodle.nottingham.ac.uk/pluginfile.php/1/mod_folder/content/3/Week%202.pptx", "Week 2.pptx")
    ]


def test_allowed_requires_https_and_exact_host():
    assert allowed("https://moodle.nottingham.ac.uk/pluginfile.php/1")
    assert not allowed("http://moodle.nottingham.ac.uk/pluginfile.php/1")
    assert not allowed("https://evil.example/?u=https://moodle.nottingham.ac.uk")


def test_safe_name_reserved_and_length():
    assert safe_name("CON") == "_CON"
    assert safe_name("   ") == "Untitled"
    assert len(safe_name("x" * 300)) == 95


def test_file_name_format_is_stable():
    # Step 6 relies on this exact digest to recognise files downloaded by organiser.py.
    url = "https://moodle.nottingham.ac.uk/pluginfile.php/1/mod_resource/content/4/notes.pdf"
    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    assert file_name("Notes.pdf", url, "application/pdf") == f"Notes-{digest}.pdf"


def test_file_name_falls_back_to_mime_type():
    url = "https://moodle.nottingham.ac.uk/pluginfile.php/1/mod_resource/content/4/download"
    assert file_name("Slides", url, "application/vnd.ms-powerpoint").endswith(".ppt")
