"""Make study notes and flashcards for every file, free, with Ollama on this Mac.

    python3 scripts/summarise.py              # all files without notes, newest first
    python3 scripts/summarise.py --limit 10   # just the 10 newest
    python3 scripts/summarise.py --model gemma3:12b --all   # a bigger model; include long books

The Study desk page has the same thing as a button (Revise > Notes for all your files).
"""

import argparse
import sys
import threading
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from student_os import db  # noqa: E402
from student_os.library.index import index_library  # noqa: E402
from student_os.library.notes import DEFAULT_MODEL, BatchProgress, Ollama, files_needing_notes, summarise_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Make study notes and flashcards with Ollama on this Mac (free)")
    parser.add_argument("--model", help=f"Ollama model to use (default {DEFAULT_MODEL}, or whatever is installed)")
    parser.add_argument("--limit", type=int, help="only the N newest files")
    parser.add_argument("--all", action="store_true", help="include very long files such as reference books")
    args = parser.parse_args()

    path = ROOT / "data" / "moodle.sqlite3"
    if not path.exists():
        print("No synced data yet. Run python3 scripts/sync_moodle.py first.")
        return 1
    ollama = Ollama(model=args.model)
    status = ollama.status()
    if not status["running"]:
        print("Ollama isn't running. Install it from https://ollama.com, open the app, and try again.")
        return 1
    if not status["model"]:
        print(f"No suitable model installed. Run: ollama pull {args.model or DEFAULT_MODEL}")
        return 1
    with closing(db.connect(path)) as conn:
        index_library(conn, ROOT)
        waiting = len(files_needing_notes(conn, include_long=args.all))
    total = min(waiting, args.limit) if args.limit else waiting
    if not total:
        print("Every file already has notes.")
        return 0
    print(f"Making notes for {total} file{'s' if total != 1 else ''} with {status['model']}. Ctrl+C stops safely.")

    progress = BatchProgress()
    shown = 0

    def report() -> None:
        nonlocal shown
        while shown < progress.done:
            shown += 1
            print(f"  {shown}/{progress.total} done", flush=True)

    worker = threading.Thread(target=summarise_all, args=(lambda: db.connect(path), ollama, progress),
                              kwargs={"include_long": args.all, "limit": args.limit})
    worker.start()
    try:
        while worker.is_alive():
            worker.join(1.0)
            report()
    except KeyboardInterrupt:
        print("\nStopping after the current file...")
        progress.stop.set()
        worker.join()
    report()
    for error in progress.errors:
        print(f"  ! {error}")
    print(f"Done: notes for {progress.done - len(progress.errors)} files. Revise them in the Study desk.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
