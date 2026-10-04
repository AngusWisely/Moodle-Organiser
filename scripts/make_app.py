"""Make "Study desk.app" so the dashboard opens with a double-click (macOS).

    python3 scripts/make_app.py

Then drag Study desk from your Applications folder to the Dock.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from student_os.dashboard.mac_app import build_app  # noqa: E402

if __name__ == "__main__":
    try:
        app = build_app(ROOT)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Couldn't make the app: {exc}")
        sys.exit(1)
    print(f"Made {app}")
    print("Drag it from that folder to your Dock. Double-click it any time to open the Study desk.")
    subprocess.run(["open", "-R", str(app)], check=False)  # show it in Finder
