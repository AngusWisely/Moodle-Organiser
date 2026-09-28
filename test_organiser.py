from organiser import Item, activities, allowed, course_links, file_name, files_from_html, process, safe_name


def test_course_links():
    html = '<a href="/course/view.php?id=123">Acoustics</a><a href="/course/view.php?id=123">Acoustics</a>'
    assert course_links(html) == [("Acoustics", "https://moodle.nottingham.ac.uk/course/view.php?id=123")]


def test_section_and_activity():
    html = '''<li class="section"><h3 class="sectionname">Lighting</h3>
        <li class="activity"><a href="/mod/resource/view.php?id=2"><span class="instancename">Lecture Notes</span></a></li>
        </li>'''
    found = activities(html, "Acoustics", "https://moodle.nottingham.ac.uk/course/view.php?id=1")
    assert [(x.section, x.title, x.type) for x in found] == [("Lighting", "Lecture Notes", "resource")]


def test_file_links_and_safety():
    html = '<a href="/pluginfile.php/1/mod_folder/content/0/intro.pdf">Intro</a>'
    assert len(files_from_html(html, "https://moodle.nottingham.ac.uk/mod/folder/view.php?id=1")) == 1
    assert not allowed("https://moodle.nottingham.ac.uk.evil.example/pluginfile.php/1")
    assert safe_name("../bad:course") == "bad-course"
    assert file_name("Lecture", "https://moodle.nottingham.ac.uk/pluginfile.php/1/lecture.pdf", "application/pdf").endswith(".pdf")


def test_new_video_is_indexed_without_request():
    item = Item("Module", "Week 1", "Recording.mp4", "resource", "https://moodle.nottingham.ac.uk/mod/resource/view.php?id=4")
    result = process(None, item, {}, videos=False, refresh=False)
    assert result[0].status == "linked video"
