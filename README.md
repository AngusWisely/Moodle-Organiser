# Moodle organiser

Keeps my University of Nottingham Moodle teaching files in one place, and tracks what is new or has changed. It is the Moodle sync foundation of a future personal "Student OS".

You sign in normally (with MFA) in a browser window it opens. It never asks for or stores a password.

## Setup

Install Python 3.11+, then from this folder:

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

## Sync

```bash
python3 scripts/sync_moodle.py
```

The first time, it asks which modules to sync; the choice is remembered. Sign in when the browser asks; the sync carries on by itself.

Each run checks every resource cheaply and downloads only files that are new or have genuinely changed (compared by SHA-256). Files go to `materials/<module>/<section>/`.

```text
Architectural Engineering Design 3
✓ 139 checked
+ 2 new
~ 1 updated
```

- **Replaced files:** when a lecturer replaces a file, the old copy is kept in a `versions/` folder beside it.
- **Removed items:** they are marked removed after a complete scan of the module; files are never deleted.
- **Failures:** one failed download does not stop the run, and never damages a file you already have.
- **Expired sign-in:** the run stops cleanly; run it again and sign in.

| Option | What it does |
|---|---|
| `--recent [DAYS]` | List what changed in the last day (or DAYS), without opening the browser |
| `--dry-run` | Check Moodle and report what would be downloaded, changing nothing |
| `--select` | Choose modules again |
| `--videos` | Download videos instead of linking them |
| `--refresh` | Download and hash every file to catch silent changes (slow; occasional use) |

`python3 organiser.py` still works and runs the same sync.

## Where things live

| Path | Contents | In Git? |
|---|---|---|
| `materials/` | Downloaded files, `versions/`, and `index.csv` (for spreadsheets) | No |
| `data/moodle.sqlite3` | What has been seen, downloaded and changed, with history | No |
| `.browser-profile/` | The browser's Moodle sign-in | No |

The first sync imported the old `materials/index.csv`, so files downloaded by the original script were recognised rather than fetched again.

This is a personal helper: Moodle layout changes, or external services such as Echo360, may need manual handling.

## Study dashboard

After syncing, run `python3 dashboard.py` and open the local address it prints (usually [http://127.0.0.1:8767](http://127.0.0.1:8767)). It shows your modules, resources, recent changes, failed items, sync status and previous versions. Filter for lecture notes, exercises or recordings, and search inside text-based PDFs for passages with page numbers. Press Ctrl+C to stop it.

The dashboard reads the existing `data/moodle.sqlite3` database and the `materials/` files. It does not sign in to Moodle. Its PDF search index is built in the background and reused on later launches. Image-only scans, diagrams and equations may not be searchable. It only listens on your own computer.

To try it without a university account:

```bash
python3 demo.py
python3 dashboard.py --materials demo-course/materials
```

The demo has fictional modules, a revised note and a failed download. `python3 measure.py` records the duration of initial PDF indexing, an unchanged repeat and one revised document in `data/measurements.json`. That benchmark tests local indexing; the sync history records the duration of each real Moodle run. Use `python3 -m pytest` to run the dashboard and sync tests.

## Development

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

Tests use a fake Moodle and never go online. Code is in `student_os/`:

| Module | Job |
|---|---|
| `moodle/session.py` | Browser, sign-in detection, course-page scanning |
| `moodle/scraper.py` | HTML parsing |
| `moodle/fetch.py` | HTTP: revision probe, downloads, login detection, redirect safety |
| `moodle/detector.py` | Pure decisions: what to fetch, what a result means |
| `moodle/storage.py` | Atomic writes, hashing, `versions/` |
| `moodle/sync.py` | Orchestration per module |
| `moodle/cli.py` | Command line and reports |
| `db.py` | SQLite schema, records and history |
