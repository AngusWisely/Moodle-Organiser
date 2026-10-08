"""Create fictional course materials to explore the dashboard without Moodle."""

from __future__ import annotations

import hashlib
from pathlib import Path

from student_os import db

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "demo-course"


def pdf_bytes(lines: list[str]) -> bytes:
    """Write a small one-page, text-selectable PDF without extra dependencies."""
    def escaped(value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    commands = ["BT /F1 14 Tf 55 760 Td"]
    for n, line in enumerate(lines):
        commands.append(f"{'0 -28 Td' if n else ''} ({escaped(line)}) Tj")
    commands.append("ET")
    stream = "\n".join(commands).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(result)
    result += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    for offset in offsets[1:]:
        result += f"{offset:010} 00000 n \n".encode()
    result += f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    return result


def create_demo(destination: Path = DEST) -> Path:
    """Create a standalone copy of the real DB layout with invented content."""
    destination = destination.resolve()
    if (destination / "data" / "moodle.sqlite3").exists():
        return destination
    materials = destination / "materials"
    materials.mkdir(parents=True, exist_ok=True)
    conn = db.connect(destination / "data" / "moodle.sqlite3")
    examples = [
        ("Building Physics", "Week 1", "Heat transfer lecture notes", "Heat transfer coefficient U-value measures heat flow through a wall."),
        ("Building Physics", "Week 2", "Insulation exercises", "Exercise: calculate heat loss for a room with a given U-value."),
        ("Sustainable Design", "Week 1", "Solar design notes", "Solar panels convert sunlight into electricity. Shading reduces output."),
    ]
    try:
        at = db.utc_now()
        with conn:
            modules = {name: db.upsert_module(conn, moodle_id=n, name=name,
                       url=f"https://example.invalid/course/{n}", at=at)
                       for n, name in enumerate(("Building Physics", "Sustainable Design"), 1)}
            db.select_modules(conn, [m.id for m in modules.values()])
            run_id = db.start_run(conn, "normal", at)
            for n, (module_name, section, title, body) in enumerate(examples, 1):
                url = f"https://example.invalid/resource/{n}"
                resource, _ = db.see_resource(conn, key=f"fictional-{n}", module_id=modules[module_name].id,
                              section=section, title=title, resource_type="resource", source_url=url, at=at)
                relative = f"materials/{module_name}/{section}/example-{n}.pdf"
                data = pdf_bytes([title, body])
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                db.save_download(conn, resource.id, file_url=url, etag=None, last_modified=None,
                                 local_path=relative, file_size=len(data), content_hash=hashlib.sha256(data).hexdigest(),
                                 at=at, changed=True)
                db.log_event(conn, run_id, resource.id, "new", at)
                if n == 1:
                    archived = target.parent / "versions" / "example-1-before.pdf"
                    archived.parent.mkdir(exist_ok=True)
                    archived.write_bytes(pdf_bytes([title, "Earlier draft: heat flows from warm to cold."]))
                    db.log_event(conn, run_id, resource.id, "updated", at,
                                 archived_path=str(archived.relative_to(destination)))
            recording, _ = db.see_resource(conn, key="fictional-recording", module_id=modules["Building Physics"].id,
                section="Week 2", title="Week 2 recording", resource_type="url",
                source_url="https://example.invalid/recording", at=at)
            db.save_status(conn, recording.id, db.VIDEO_LINK)
            missing, _ = db.see_resource(conn, key="fictional-missing", module_id=modules["Sustainable Design"].id,
                section="Week 2", title="Materials worksheet", resource_type="resource",
                source_url="https://example.invalid/missing", at=at)
            db.save_failure(conn, missing.id, "TimeoutError: example download failed")
            db.log_event(conn, run_id, missing.id, "failed", at, message="TimeoutError: example download failed")
            db.finish_run(conn, run_id, "completed_with_errors", db.utc_now(),
                          bytes_received=sum(path.stat().st_size for path in materials.rglob("*.pdf")),
                          selected_count=2, scanned_count=2)
        db.export_index_csv(conn, materials / "index.csv")
    finally:
        conn.close()
    return destination


if __name__ == "__main__":
    print(f"Fictional course ready: {create_demo()}")
    print("Run: python3 dashboard.py --materials demo-course/materials")
