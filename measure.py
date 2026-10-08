"""Repeatable offline measurements for the fictional demo, not a Moodle benchmark."""

from __future__ import annotations

import time
import json
import tempfile
from pathlib import Path

from dashboard import index_pdfs, search
from demo import create_demo, pdf_bytes


def timed(fn):
    start = time.perf_counter()
    result = fn()
    return {"seconds": round(time.perf_counter() - start, 4), **result}


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        materials = create_demo(Path(folder) / "demo") / "materials"
        first = timed(lambda: index_pdfs(materials))
        repeat = timed(lambda: index_pdfs(materials))
        revised = materials / "Building Physics" / "Week 1" / "example-1.pdf"
        revised.write_bytes(pdf_bytes(["Heat transfer lecture notes", "Updated: heat transfer coefficient includes surface resistance."]))
        changed = timed(lambda: index_pdfs(materials))
        results = search(materials, "heat transfer coefficient")
        report = {"scope": "fictional PDFs and local text indexing; no Moodle network timing",
                  "initial_index": first, "unchanged_repeat": repeat, "one_changed_pdf": changed,
                  "change_detected_correctly": first["indexed"] == 3 and repeat["indexed"] == 0 and changed["indexed"] == 1,
                  "search_returns_revised_page": bool(results and results[0][3] == "1" and "Updated" in results[0][4]),
                  "fixture_file_bytes": sum(path.stat().st_size for path in materials.rglob("*.pdf"))}
    target = Path(__file__).resolve().parent / "data" / "measurements.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(report)
    print(f"Saved {target}")


if __name__ == "__main__":
    main()
