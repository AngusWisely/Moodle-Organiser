"""Open the study dashboard in your browser: python3 scripts/dashboard.py"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from student_os.dashboard.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(ROOT))
