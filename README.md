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

## Development

The code is moving into the `student_os/` package (parsing in `student_os/moodle/scraper.py`, naming in `storage.py`, URL rules in `urls.py`). `organiser.py` still works exactly as before.

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

Tests never contact Moodle. `data/` (the upcoming sync database) is ignored by Git, like `materials/`.
