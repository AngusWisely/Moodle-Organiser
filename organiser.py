"""Old entry point, kept so ``python3 organiser.py`` still works. It now runs the new sync.

Use ``python3 scripts/sync_moodle.py`` instead; it accepts the old ``--videos``
and ``--refresh`` options plus ``--dry-run``, ``--recent`` and ``--select``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from student_os.moodle import cli  # noqa: E402

NOTICE = "organiser.py now runs the new sync (python3 scripts/sync_moodle.py)."


def main(argv: list[str] | None = None) -> int:
    print(NOTICE)
    return cli.main(ROOT, sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Everything synced so far is saved.")
        sys.exit(130)
