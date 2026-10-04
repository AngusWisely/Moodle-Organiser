"""Sign-in: press what a person would (saved-login form, then university SSO), each once; else wait."""

import pytest

from student_os.moodle.session import (
    LOGIN_BUTTON,
    SSO_BUTTONS,
    USERNAME_FIELD,
    press_university_login,
    sign_in,
    signed_in_url,
    submit_saved_login,
)
from tests.fakes import M

LOGIN = f"{M}/login/index.php"
MY = f"{M}/my/courses.php"
MICROSOFT = "https://login.microsoftonline.com/x/saml2"


class FakeElement:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def count(self):
        return int(self.selector in self.page.present)

    def input_value(self):
        return self.page.username

    def click(self, timeout):
        self.page.clicks.append(self.selector)
        target = self.page.on_click.get(self.selector)
        if target:
            self.page.url = target


class FakePage:
    """Moodle's login page: ``on_click`` says where each button leads."""

    def __init__(self, start=LOGIN, *, username="efyaw5", on_click=None, present=None):
        self.url, self.username = start, username
        self.on_click = {LOGIN_BUTTON: MY, SSO_BUTTONS: MICROSOFT} if on_click is None else on_click
        self.present = {USERNAME_FIELD, LOGIN_BUTTON, SSO_BUTTONS} if present is None else present
        self.clicks: list[str] = []
        self.waits: list[int] = []

    def goto(self, url, **kwargs):
        pass

    def locator(self, selector):
        return FakeElement(self, selector)

    def wait_for_url(self, predicate, timeout):
        self.waits.append(timeout)
        if not predicate(self.url):
            raise TimeoutError("still not signed in")


def test_already_signed_in_does_nothing():
    page = FakePage(MY)
    sign_in(page)
    assert page.clicks == [] and page.waits == []


def test_saved_browser_login_signs_in_by_itself(capsys):
    page = FakePage()
    sign_in(page)
    assert page.clicks == [USERNAME_FIELD, LOGIN_BUTTON]  # never touches the SSO button
    assert "saved browser login" in capsys.readouterr().out


def test_empty_form_is_never_submitted_and_sso_is_tried():
    page = FakePage(username="", on_click={SSO_BUTTONS: MY})
    sign_in(page)
    assert LOGIN_BUTTON not in page.clicks and SSO_BUTTONS in page.clicks


def test_rejected_login_is_not_retried_then_waits_for_user(capsys):
    page = FakePage(on_click={SSO_BUTTONS: MICROSOFT})  # form login fails, Microsoft needs the user
    with pytest.raises(TimeoutError):
        sign_in(page, timeout_s=1, auto_wait_ms=10)
    assert page.clicks.count(LOGIN_BUTTON) == 1 and page.clicks.count(SSO_BUTTONS) == 1
    assert page.waits == [10, 10, 1000]
    assert "Waiting" in capsys.readouterr().out


def test_buttons_only_pressed_on_moodle_login_page():
    assert not press_university_login(FakePage(MICROSOFT))
    assert not press_university_login(FakePage(f"{M}/course/view.php?id=1"))
    assert not press_university_login(FakePage(present=set()))
    assert not submit_saved_login(FakePage(present={USERNAME_FIELD}))  # no button, no click


def test_signed_in_url():
    assert signed_in_url(MY) and signed_in_url(f"{M}/my/")
    assert not signed_in_url(LOGIN) and not signed_in_url("https://evil.example/my/")
