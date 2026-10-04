"""Build "Study desk.app" for the Dock (macOS), so the dashboard opens with a double-click.

The app is a tiny AppleScript (compiled with macOS's own ``osacompile``) that
runs ``scripts/dashboard.py --background`` with the same Python that built it,
and shows any problem in a normal Mac dialog. Rebuild it if you move the
project folder or change Python.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_NAME = "Study desk"
ICON_SIZES = (16, 32, 128, 256, 512)


def applescript_string(text: str) -> str:
    """A safely quoted AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def launcher_script(root: Path, python: str) -> str:
    command = (f"\"cd \" & quoted form of {applescript_string(str(root))} & \" && \" & "
               f"quoted form of {applescript_string(python)} & \" scripts/dashboard.py --background 2>&1\"")
    return (
        "on run\n"
        "\ttry\n"
        f"\t\tdo shell script {command}\n"
        "\ton error errText\n"
        f"\t\tdisplay dialog errText buttons {{\"OK\"}} default button \"OK\" with title {applescript_string(APP_NAME)}"
        " with icon caution\n"
        "\tend try\n"
        "end run\n"
    )


def draw_icon(size: int = 1024):
    """The Study desk mark: a drawing sheet with a chart curve, in the dashboard's colours."""
    from PIL import Image, ImageDraw

    scale = size / 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    s = lambda v: round(v * scale)  # noqa: E731
    draw.rounded_rectangle((s(80), s(80), s(944), s(944)), radius=s(190), fill=(30, 42, 58, 255))
    draw.rounded_rectangle((s(190), s(190), s(834), s(834)), radius=s(60), fill=(243, 245, 242, 255))
    for y in (370, 500, 630):  # ruled lines
        draw.line((s(250), s(y), s(774), s(y)), fill=(203, 213, 209, 255), width=max(1, s(14)))
    curve = [(250 + t * 524, 760 - 520 * (t ** 2.2)) for t in [i / 40 for i in range(41)]]  # saturation-style curve
    draw.line([(s(x), s(y)) for x, y in curve], fill=(15, 123, 108, 255), width=max(2, s(46)), joint="curve")
    return image


def build_app(root: Path, *, destination: Path | None = None, python: str | None = None,
              run=subprocess.run) -> Path:
    """Create (or replace) the app. Returns its path. macOS only."""
    if sys.platform != "darwin" and run is subprocess.run:
        raise RuntimeError("The Dock app can only be built on a Mac")
    destination = destination or Path.home() / "Applications"
    destination.mkdir(parents=True, exist_ok=True)
    app = destination / f"{APP_NAME}.app"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "launcher.applescript"
        script.write_text(launcher_script(root.resolve(), python or sys.executable), encoding="utf-8")
        if app.exists():
            shutil.rmtree(app)
        run(["osacompile", "-o", str(app), str(script)], check=True)
        _set_icon(app, Path(tmp), run)
    return app


def _set_icon(app: Path, tmp: Path, run) -> None:
    """Swap the default script icon for ours (skipped quietly if macOS tools are missing)."""
    if shutil.which("iconutil") is None and run is subprocess.run:
        return
    iconset = tmp / "StudyDesk.iconset"
    iconset.mkdir()
    big = draw_icon(1024)
    for size in ICON_SIZES:
        big.resize((size, size)).save(iconset / f"icon_{size}x{size}.png")
        big.resize((size * 2, size * 2)).save(iconset / f"icon_{size}x{size}@2x.png")
    target = app / "Contents" / "Resources" / "applet.icns"
    try:
        run(["iconutil", "-c", "icns", str(iconset), "-o", str(target)], check=True)
        run(["touch", str(app)], check=False)  # make Finder notice the new icon
    except (OSError, subprocess.CalledProcessError):
        pass
