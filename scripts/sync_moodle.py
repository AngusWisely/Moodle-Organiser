"""Sync Nottingham Moodle into materials/. Run from anywhere: python scripts/sync_moodle.py --help"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from student_os.moodle.cli import main  # noqa: E402

if __name__ == "__main__":
    try:
        sys.exit(main(ROOT))
    except KeyboardInterrupt:
        print("\nStopped. Everything synced so far is saved.")
        sys.exit(130)
