"""Sign-in: press Moodle's university login button once, otherwise wait for the user."""

import pytest

from student_os.moodle.session import SSO_BUTTONS, press_university_login, sign_in, signed_in_url
from tests.fakes import M

LOGIN = f"{M}/login/index.php"
MY = f"{M}/my/courses.php"


class FakeLocator:
    def __init__(self, page, count):
        self.page, self._count = page, count

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def click(self, timeout):
        self.page.clicks += 1
        self.page.url = self.page.after_click


class FakePage:
    """Lands on ``start``; clicking the SSO button moves to ``after_click``."""

    def __init__(self, start, *, buttons=1, after_click=MY):
        self.url, self.buttons, self.after_click = start, buttons, after_click
        self.clicks = 0
        self.waits = []

    def goto(self, url, **kwargs):
        pass

    def locator(self, selector):
        assert selector == SSO_BUTTONS
        return FakeLocator(self, self.buttons)

    def wait_for_url(self, predicate, timeout):
        self.waits.append(timeout)
        if not predicate(self.url):
            raise TimeoutError("still not signed in")


def test_already_signed_in_does_nothing():
    page = FakePage(MY)
    sign_in(page)
    assert page.clicks == 0 and page.waits == []


def test_remembered_university_sign_in_needs_no_one(capsys):
    page = FakePage(LOGIN)
    sign_in(page)
    assert page.clicks == 1 and "automatically" in capsys.readouterr().out


def test_falls_back_to_waiting_when_mfa_is_needed(capsys):
    page = FakePage(LOGIN, after_click="https://login.microsoftonline.com/mfa")
    with pytest.raises(TimeoutError):
        sign_in(page, timeout_s=1, auto_wait_ms=10)
    assert page.clicks == 1 and page.waits == [10, 1000]  # clicked once, then waited for the user
    assert "Waiting" in capsys.readouterr().out


def test_never_clicks_outside_moodle_login_page():
    assert not press_university_login(FakePage("https://login.microsoftonline.com/x"))
    assert not press_university_login(FakePage(f"{M}/course/view.php?id=1"))
    assert not press_university_login(FakePage(LOGIN, buttons=0))


def test_signed_in_url():
    assert signed_in_url(MY) and signed_in_url(f"{M}/my/")
    assert not signed_in_url(LOGIN) and not signed_in_url("https://evil.example/my/")
