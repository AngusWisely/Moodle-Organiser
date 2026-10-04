# Moodle organiser

A small Python script I made to keep my University of Nottingham Moodle files in one place. It opens a browser so I can sign in normally, then downloads available teaching files into module folders and makes a searchable `index.csv`.

I run it every week or so to pick up new notes. It skips files already downloaded and leaves new videos as links by default.

## Run it

Install Python 3.11+ and then, from this folder:

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 organiser.py
```

Sign in to Moodle in the browser window, return to Terminal, press Enter, and choose the modules to scan. Files and the index appear in `materials/`.

`python3 organiser.py --videos` also downloads new MP4s. `python3 organiser.py --refresh` re-downloads files if a lecturer has replaced one at the same Moodle link.

The script keeps a local browser profile for sign-in, but never asks for a password. `materials/` and `.browser-profile/` are ignored by Git so course documents and sign-in data stay off GitHub.

This is a personal helper, so Moodle layout changes or external services such as Echo360 may need manual handling.

## Daily sync (new)

```bash
python3 scripts/sync_moodle.py
```

The first run imports your existing `materials/index.csv`, so files already downloaded are not fetched again, and asks which modules to sync (remembered; change with `--select`). Sign in when the browser asks; there is no need to press Enter.

Each run checks every resource cheaply and downloads only what is new or has genuinely changed (compared by SHA-256). When a lecturer replaces a file, the old copy is kept in a `versions/` folder beside it. Items that disappear from Moodle are marked removed; their files are never deleted.

| Option | What it does |
|---|---|
| `--dry-run` | Check Moodle and report, changing nothing |
| `--recent [DAYS]` | List what changed in the last day (or DAYS), without opening the browser |
| `--refresh` | Download and hash everything to catch silent changes (slow; occasional use) |
| `--videos` | Download videos instead of linking them |
| `--select` | Choose modules again |

State lives in `data/moodle.sqlite3`; `materials/index.csv` is regenerated from it for spreadsheets.

## Development

Code lives in `student_os/`: `moodle/scraper.py` (HTML parsing), `session.py` (browser and sign-in), `fetch.py` (HTTP), `detector.py` (change decisions), `storage.py` (files and versions), `sync.py` (orchestration), and `db.py` (SQLite). `organiser.py` still works as before.

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

Tests never contact Moodle. `data/` is ignored by Git, like `materials/`.
