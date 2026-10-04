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

## Study desk (dashboard)

```bash
python3 scripts/dashboard.py
```

Opens a page in your browser, built from your synced files:

- **Coming up:** deadlines and exams found in briefs and handbooks, on a week-by-week ruler, each with the sentence it came from.
- **New and updated:** what the sync fetched recently, by day.
- **Search inside your files:** every word of your PDFs, slides and Word files. A result in a PDF opens at that page.
- **Each file's outline:** slide titles and headings as a table of contents, plus any dates it mentions.

**Open it without Terminal:** run `python3 scripts/make_app.py` once. It puts **Study desk** in your Applications folder; drag it to the Dock. Double-click it any time: it opens the page, starting the dashboard quietly in the background if it isn't already running. The background dashboard switches itself off after three hours unused. (If you move this folder, run `make_app.py` again.)

It runs only on your computer (127.0.0.1) and needs no internet. Press Ctrl+C in Terminal to close it. The sync keeps the search index up to date; files are read again only when they change.

## Study notes and flashcards (free)

Each file's page in the Study desk can give you a summary, key points and flashcards, in two free ways:

- **Summarise on this Mac** uses [Ollama](https://ollama.com), a free app that runs an AI model on your own computer. Nothing is sent anywhere. One-off setup: install Ollama, open it, then run `ollama pull gemma3:4b` (a 3.3 GB download). To do every file at once, use **Revise flashcards → Make notes for all files**, or `python3 scripts/summarise.py`.
- **Copy for Claude** copies the file's text with a ready-made request. Paste it into Claude, then paste Claude's reply back into the box and press **Save notes**.

Notes are made once per version of a file; if a lecturer replaces the file, its notes are marked out of date.

**Revise flashcards** shows the cards due today, one at a time: press space to see the answer, then 1 (again), 2 (hard), 3 (good) or 4 (easy). Cards you know come back after longer and longer gaps; ones you forget come back sooner.

## Where things live

| Path | Contents | In Git? |
|---|---|---|
| `materials/` | Downloaded files, `versions/`, and `index.csv` (for spreadsheets) | No |
| `data/moodle.sqlite3` | What has been seen, downloaded and changed, with history | No |
| `.browser-profile/` | The browser's Moodle sign-in | No |

The first sync imported the old `materials/index.csv`, so files downloaded by the original script were recognised rather than fetched again.

This is a personal helper: Moodle layout changes, or external services such as Echo360, may need manual handling.

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
| `library/` | Text extraction, outlines, dates, full-text search, notes (Ollama / Claude), flashcard scheduling |
| `dashboard/` | Local web server and the study desk page |
| `db.py` | SQLite schema, records and history |
