"""The Dock app: start the dashboard quietly if needed, reuse it if running, stop it when idle."""

import threading
import time

import pytest

from student_os import db
from student_os.dashboard import cli
from student_os.dashboard.mac_app import applescript_string, build_app, draw_icon, launcher_script
from student_os.dashboard.server import Dashboard


@pytest.fixture
def project(tmp_path):
    db.connect(tmp_path / "data" / "moodle.sqlite3").close()
    return tmp_path


def start_server_thread(root):
    threading.Thread(target=cli.serve, kwargs={"root": root, "port": 0, "open_browser": False}, daemon=True).start()


def test_starts_dashboard_when_none_is_running(project):
    spawned, opened = [], []
    code = cli.open_in_background(project, 0, opener=opened.append,
                                  spawn=lambda root, port: (spawned.append(port), start_server_thread(root)))
    assert code == 0 and spawned == [0]
    port = int(cli.port_file(project).read_text())
    assert opened == [f"http://127.0.0.1:{port}/"] and cli.ping(port)


def test_reuses_a_running_dashboard(project):
    start_server_thread(project)
    for _ in range(40):
        if cli.running_port(project):
            break
        time.sleep(0.05)
    opened = []
    code = cli.open_in_background(project, 0, opener=opened.append,
                                  spawn=lambda root, port: pytest.fail("should not start a second one"))
    assert code == 0 and len(opened) == 1


def test_stale_port_file_is_ignored(project):
    cli.port_file(project).write_text("1")  # nothing listens there
    assert cli.running_port(project) is None


def test_reports_when_it_cannot_start(project, monkeypatch, capsys):
    monkeypatch.setattr(cli, "START_TIMEOUT_S", 0.3)
    assert cli.open_in_background(project, 0, opener=lambda url: pytest.fail("nothing to open"),
                                  spawn=lambda root, port: None) == 1
    assert "dashboard.log" in capsys.readouterr().out


def test_background_dashboard_stops_when_idle(project):
    app = Dashboard(project)
    server = app.make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app.last_request = time.monotonic() - 10
    cli._stop_when_idle(app, server, 0.1)
    thread.join(2)
    assert not thread.is_alive()
    server.server_close()


def test_main_needs_synced_data(tmp_path, capsys):
    assert cli.main(tmp_path, ["--background"]) == 1
    assert "sync_moodle.py" in capsys.readouterr().out


# --- the macOS app -----------------------------------------------------------------------

def test_applescript_quotes_paths_safely():
    assert applescript_string('a "b" \\c') == '"a \\"b\\" \\\\c"'
    script = launcher_script(__import__("pathlib").Path('/Users/me/My "Uni" Files'), "/usr/local/bin/python3")
    assert 'quoted form of "/Users/me/My \\"Uni\\" Files"' in script
    assert "scripts/dashboard.py --background 2>&1" in script and "display dialog errText" in script


def test_build_app_compiles_script_and_sets_icon(tmp_path):
    calls = []

    def fake_run(args, check):
        calls.append(args)
        if args[0] == "osacompile":
            (tmp_path / "Applications" / "Study desk.app" / "Contents" / "Resources").mkdir(parents=True)
        if args[0] == "iconutil":
            assert sorted(p.name for p in (tmp_path / "Applications").parent.glob("*"))  # iconset was written
            open(args[-1], "wb").close()

    app = build_app(tmp_path / "project", destination=tmp_path / "Applications", python="/usr/bin/python3", run=fake_run)
    assert app == tmp_path / "Applications" / "Study desk.app"
    assert [c[0] for c in calls] == ["osacompile", "iconutil", "touch"]
    assert (app / "Contents" / "Resources" / "applet.icns").exists()


def test_icon_draws_at_every_size():
    for size in (16, 1024):
        image = draw_icon(size)
        assert image.size == (size, size) and image.getpixel((size // 2, size // 2))[3] == 255
