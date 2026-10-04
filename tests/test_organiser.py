"""organiser.py is now a thin wrapper that runs the new sync."""

import organiser


def test_organiser_forwards_old_options_to_the_new_sync(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(organiser.cli, "main", lambda root, argv: calls.append((root, argv)) or 0)
    assert organiser.main(["--videos", "--refresh"]) == 0
    assert calls == [(organiser.ROOT, ["--videos", "--refresh"])]
    assert "scripts/sync_moodle.py" in capsys.readouterr().out


def test_old_options_are_accepted_by_the_new_cli():
    args = organiser.cli.parse_args(["--videos", "--refresh"])
    assert args.videos and args.refresh and not args.dry_run
