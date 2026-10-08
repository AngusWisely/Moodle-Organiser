"""Local study dashboard over the existing Student OS sync database."""

from __future__ import annotations

import argparse
import csv
import html
import re
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent

CSS = """
*{box-sizing:border-box}body{font:16px/1.5 system-ui,sans-serif;color:#243043;background:#f5f7fa;margin:0}
header{background:#17324d;color:white;padding:24px max(24px,calc((100% - 1100px)/2))}
header h1{margin:0;font-size:1.8rem}header p{margin:5px 0 0;color:#dce7f0}
main{max-width:1100px;margin:24px auto;padding:0 20px}h2{font-size:1.18rem;margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
.card{background:white;border:1px solid #dde5ec;border-radius:10px;padding:18px;margin-bottom:16px}
.notice{background:#fff4d8;border-color:#e8ce87}.bad{background:#fff0ee;border-color:#e9b8b1}
.muted{color:#5d6977}.small{font-size:.86rem}.pill{display:inline-block;padding:2px 9px;border-radius:20px;background:#e8eff6;margin:2px}
a{color:#075a9f}a:hover{text-decoration:underline}input,select,button{font:inherit;padding:9px;border:1px solid #9eacbb;border-radius:6px}
button{background:#173f61;color:white;cursor:pointer}.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.filters input{flex:2;min-width:210px}.filters select{flex:1;min-width:150px}
ul{padding-left:20px;margin:8px 0}li{margin:7px 0}mark{background:#fff2a6}
.resource{border-top:1px solid #e4e9ee;padding:12px 0}.resource:first-of-type{border:0}
.resource strong{display:block}.tag{font-size:.76rem;text-transform:uppercase;letter-spacing:.04em;color:#596979}
.stats{font-size:1.45rem;font-weight:700}details{margin-top:10px}summary{cursor:pointer}code{overflow-wrap:anywhere}
"""


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def size_label(size: int) -> str:
    return f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size / 1024:.0f} KB"


def load_rows(materials: Path) -> list[dict[str, str]]:
    db_path = materials.parent / "data" / "moodle.sqlite3"
    if db_path.exists():
        with sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT m.name AS module, r.section, r.title, r.resource_type AS type, "
                "r.source_url AS url, r.status, COALESCE(r.local_path,'') AS local_file, "
                "COALESCE(r.last_error,'') AS last_error FROM resources r "
                "JOIN modules m ON m.id=r.module_id ORDER BY m.name,r.section,r.title")]
    path = materials / "index.csv"  # usable before the first run of the new sync
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))
    return []


def sync_state(materials: Path) -> dict:
    path = materials.parent / "data" / "moodle.sqlite3"
    if not path.exists():
        return {}
    with sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        latest = db.execute("SELECT * FROM sync_runs WHERE mode != 'import' AND mode != 'dry_run' ORDER BY id DESC LIMIT 1").fetchone()
        success = db.execute("SELECT finished_at FROM sync_runs WHERE outcome='completed' AND mode != 'import' AND mode != 'dry_run' ORDER BY id DESC LIMIT 1").fetchone()
        if not latest:
            return {}
        errors = [r[0] for r in db.execute("SELECT m.name || ': ' || r.title || ': ' || r.last_error "
                  "FROM resources r JOIN modules m ON m.id=r.module_id WHERE r.last_error IS NOT NULL ORDER BY m.name,r.title")]
        return {"status": latest["outcome"], "started_at": latest["started_at"],
                "finished_at": latest["finished_at"], "last_success_at": success[0] if success else None,
                "new_files": db.execute("SELECT count(*) FROM sync_events WHERE run_id=? AND kind='new'", (latest["id"],)).fetchone()[0],
                "updated_files": db.execute("SELECT count(*) FROM sync_events WHERE run_id=? AND kind='updated'", (latest["id"],)).fetchone()[0],
                "bytes_received": latest["bytes_received"] if "bytes_received" in latest.keys() else 0,
                "selected_count": latest["selected_count"] if "selected_count" in latest.keys() else 0,
                "scanned_count": latest["scanned_count"] if "scanned_count" in latest.keys() else 0,
                "errors": errors}


def versions_for(materials: Path) -> list[dict]:
    path = materials.parent / "data" / "moodle.sqlite3"
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(
            "SELECT m.name AS module,r.source_url AS url,r.title,e.created_at AS saved_at,e.archived_path AS local_file "
            "FROM sync_events e JOIN resources r ON r.id=e.resource_id JOIN modules m ON m.id=r.module_id "
            "WHERE e.archived_path IS NOT NULL ORDER BY e.id")]


def safe_file(materials: Path, relative: str) -> Path | None:
    if not relative:
        return None
    # Index paths are relative to the project root; version paths are relative to materials.
    path = materials / (relative.removeprefix("materials/") if relative.startswith("materials/") else relative)
    resolved = path.resolve()
    if resolved.is_relative_to(materials.resolve()) and resolved.is_file():
        return resolved
    return None


def category(row: dict[str, str]) -> str:
    title = row.get("title", "").lower()
    suffix = Path(row.get("local_file", "")).suffix.lower()
    if "recording" in title or suffix in {".mp4", ".mov", ".m4v"} or "video" in row.get("status", ""):
        return "recordings"
    if any(word in title for word in ("exercise", "question", "problem", "solution", "quiz", "worksheet")):
        return "exercises"
    if any(word in title for word in ("lecture", "slide", "presentation", "notes")):
        return "lecture notes"
    return "other"


def events(materials: Path) -> list[dict]:
    path = materials.parent / "data" / "moodle.sqlite3"
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(
            "SELECT e.kind,e.created_at AS at,m.name AS module,r.title,e.message AS detail "
            "FROM sync_events e JOIN resources r ON r.id=e.resource_id JOIN modules m ON m.id=r.module_id "
            "ORDER BY e.id DESC LIMIT 12")]


def init_db(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS indexed(path TEXT PRIMARY KEY, size INTEGER, modified INTEGER, error TEXT)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(module, title, path UNINDEXED, page UNINDEXED, body)")


def index_pdfs(materials: Path) -> dict[str, int]:
    """Incrementally index text PDFs; skip unchanged files and report extraction errors."""
    from pypdf import PdfReader

    stats = {"indexed": 0, "unchanged": 0, "errors": 0}
    db = sqlite3.connect(materials / ".search.sqlite", timeout=30)
    try:
        init_db(db)
        pdfs = {}
        for row in load_rows(materials):
            path = safe_file(materials, row.get("local_file", ""))
            if path and path.suffix.lower() == ".pdf":
                pdfs[str(path)] = (row, path)
        for pathname, (row, path) in pdfs.items():
            meta = path.stat()
            old = db.execute("SELECT size, modified, error FROM indexed WHERE path=?", (pathname,)).fetchone()
            if old and old[:2] == (meta.st_size, meta.st_mtime_ns):
                stats["errors" if old[2] else "unchanged"] += 1
                with db:
                    db.execute("UPDATE pages SET module=?, title=? WHERE path=?",
                               (row.get("module", ""), row.get("title", ""), pathname))
                continue
            try:
                reader = PdfReader(path)
                pages = [(row.get("module", ""), row.get("title", ""), pathname, str(n), page.extract_text() or "")
                         for n, page in enumerate(reader.pages, 1)]
                with db:
                    db.execute("DELETE FROM pages WHERE path=?", (pathname,))
                    db.executemany("INSERT INTO pages(module,title,path,page,body) VALUES(?,?,?,?,?)", pages)
                    db.execute("INSERT OR REPLACE INTO indexed VALUES(?,?,?,?)", (pathname, meta.st_size, meta.st_mtime_ns, ""))
                stats["indexed"] += 1
            except Exception as exc:
                with db:
                    db.execute("DELETE FROM pages WHERE path=?", (pathname,))
                    db.execute("INSERT OR REPLACE INTO indexed VALUES(?,?,?,?)", (pathname, meta.st_size, meta.st_mtime_ns, type(exc).__name__))
                stats["errors"] += 1
        for (old_path,) in db.execute("SELECT path FROM indexed").fetchall():
            if old_path not in pdfs:
                with db:
                    db.execute("DELETE FROM pages WHERE path=?", (old_path,))
                    db.execute("DELETE FROM indexed WHERE path=?", (old_path,))
    finally:
        db.close()
    return stats


def snippet(body: str, terms: list[str]) -> str:
    match = next((m for word in terms if (m := re.search(re.escape(word), body, re.I))), None)
    if not match:
        return esc(body[:190].replace("\n", " "))
    start, end = max(0, match.start() - 75), min(len(body), match.end() + 110)
    fragment = body[start:end].replace("\n", " ")
    return ("…" if start else "") + esc(fragment) + ("…" if end < len(body) else "")


def search(materials: Path, query: str, module: str = "") -> list[tuple]:
    terms = re.findall(r"[\w]+", query, re.UNICODE)[:8]
    if not terms or not (materials / ".search.sqlite").exists():
        return []
    expression = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
    db = sqlite3.connect(materials / ".search.sqlite", timeout=30)
    try:
        sql = "SELECT module,title,path,page,body FROM pages WHERE body MATCH ?"
        args = [expression]
        if module:
            sql += " AND module=?"
            args.append(module)
        sql += " ORDER BY rank LIMIT 40"
        return [(m, t, p, number, snippet(body, terms)) for m, t, p, number, body in db.execute(sql, args)]
    finally:
        db.close()


def render(materials: Path, params: dict[str, list[str]], indexing: dict) -> str:
    rows = load_rows(materials)
    state = sync_state(materials)
    versions = versions_for(materials)
    query = params.get("q", [""])[0].strip()[:150]
    module = params.get("module", [""])[0]
    kind = params.get("kind", [""])[0]
    modules = sorted({r.get("module", "") for r in rows if r.get("module")})
    shown = [r for r in rows if (not module or module == r.get("module")) and (not kind or kind == category(r))]
    errors = [r for r in rows if r.get("last_error") or r.get("status") == "pending"]
    last = state.get("last_success_at") or "No fully completed sync recorded"
    latest = state.get("status", "No sync status recorded")
    try:
        elapsed = round((datetime.fromisoformat(state["finished_at"]) - datetime.fromisoformat(state["started_at"])).total_seconds())
        duration = f"{elapsed}s"
    except (KeyError, TypeError, ValueError):
        duration = "unknown"
    content = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
               '<title>Moodle organiser</title><style>' + CSS + '</style>',
               '<header><h1>My study materials</h1><p>Local library · files and index stay on this computer</p></header><main>']
    content.append('<div class="grid">')
    for label, value in (("Modules", len(modules)), ("Resources indexed", len(rows)), ("Files available", sum(bool(safe_file(materials, r.get("local_file", ""))) for r in rows)), ("Failed items", len(errors))):
        content.append(f'<section class="card"><span class="muted">{esc(label)}</span><div class="stats">{value}</div></section>')
    content.append('</div>')
    warning = latest in {"aborted", "auth_failed", "completed_with_errors", "running"}
    content.append(f'<section class="card {"bad" if warning else ""}"><h2>Sync status: {esc(latest)}</h2>'
                   f'<div>Last successful scan: {esc(last)} · Latest run: {esc(state.get("started_at") or "unknown")}</div>'
                   f'<div class="muted small">Latest scan took {esc(duration)} and covered {state.get("scanned_count", 0)} of {state.get("selected_count", 0)} selected modules. '
                   f'Added {int(state.get("new_files", 0))} files, revised {int(state.get("updated_files", 0))}; '
                   f'received {esc(size_label(int(state.get("bytes_received", 0))))}.</div>'
                   '<p class="small">To recover: run <code>python3 organiser.py</code> again and sign in if asked. '
                   'Use <code>python3 organiser.py --select</code> to include a newly released module. '
                   'The usual scan checks known revisions; <code>python3 organiser.py --refresh</code> downloads every file to catch silent replacements.</p></section>')
    if state.get("errors"):
        content.append('<section class="card bad"><h2>Scan problems</h2><ul>' + ''.join(f'<li>{esc(e)}</li>' for e in state["errors"][:20]) + '</ul></section>')
    if indexing.get("running") or indexing.get("error"):
        content.append(f'<section class="card notice">PDF indexing: {esc(indexing.get("error") or "In progress. Search will fill in shortly; refresh this page.")}</section>')
    if indexing.get("stats", {}).get("errors"):
        content.append(f'<section class="card notice">PDF text could not be extracted from {indexing["stats"]["errors"]} file(s). Image-only PDFs also have no searchable text.</section>')
    content.append('<section class="card"><h2>Find something</h2><form class="filters" method="get">'
                   f'<input name="q" placeholder="Search inside PDFs" value="{esc(query)}" aria-label="Search PDF text">'
                   '<select name="module" aria-label="Module"><option value="">All modules</option>')
    content.extend(f'<option value="{esc(m)}" {"selected" if m == module else ""}>{esc(m)}</option>' for m in modules)
    content.append('</select><select name="kind" aria-label="Resource type"><option value="">All types</option>')
    content.extend(f'<option value="{esc(k)}" {"selected" if k == kind else ""}>{esc(k.title())}</option>' for k in ("lecture notes", "exercises", "recordings", "other"))
    content.append('</select><button>Search</button></form>')
    if query:
        try:
            matches = search(materials, query, module)
            if kind:
                kinds_by_path = {str(p): category(r) for r in rows
                                 if (p := safe_file(materials, r.get("local_file", "")))}
                matches = [match for match in matches if kinds_by_path.get(match[2]) == kind]
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            matches = []
        content.append(f'<h2>PDF matches ({len(matches)} shown)</h2>')
        for m, title, path, page, passage in matches:
            relative = str(Path(path).relative_to(materials)) if Path(path).is_relative_to(materials) else ""
            file_url = '/file?path=' + quote(relative) + '#page=' + quote(page)
            content.append(f'<div class="resource"><strong><a href="{esc(file_url)}">{esc(title)} · page {esc(page)}</a></strong>'
                           f'<span class="tag">{esc(m)}</span><div>{passage}</div></div>')
        if not matches:
            content.append('<p class="muted">No text matches found. Image-only scans, diagrams and equations may not be searchable.</p>')
    content.append('</section>')
    content.append('<div class="grid"><section class="card"><h2>Recent changes</h2><ul>')
    for event in events(materials):
        content.append(f'<li><b>{esc(event.get("kind", ""))}</b> · {esc(event.get("title", ""))}'
                       f'<div class="small muted">{esc(event.get("module", ""))} · {esc(event.get("at", ""))}</div></li>')
    content.append('</ul></section><section class="card"><h2>Needs attention</h2><ul>')
    for row in errors[:12]:
        content.append(f'<li>{esc(row.get("title", ""))}<div class="small muted">{esc(row.get("last_error") or row.get("status", ""))}</div></li>')
    if not errors:
        content.append('<li>No failed items in the current index.</li>')
    content.append('</ul></section></div>')
    content.append(f'<section class="card"><h2>Resources ({len(shown)})</h2>')
    for row in shown:
        local = row.get("local_file", "")
        path = safe_file(materials, local)
        if path:
            href = '/file?path=' + quote(str(path.relative_to(materials)))
        else:
            url = row.get("url", "")
            href = url if urlparse(url).scheme == "https" else "#"
        content.append(f'<div class="resource"><strong><a href="{esc(href)}">{esc(row.get("title", "Untitled"))}</a></strong>'
                       f'<span class="tag">{esc(row.get("module"))} · {esc(row.get("section"))} · {esc(category(row))}</span>'
                       f'<div class="small muted">{esc(row.get("status"))}</div>')
        history = [v for v in versions if v.get("module") == row.get("module") and v.get("url") == row.get("url")]
        if history:
            content.append(f'<details><summary>Previous versions ({len(history)})</summary><ul>')
            for v in reversed(history):
                old = safe_file(materials, v.get("local_file", ""))
                if old:
                    content.append(f'<li><a href="/file?path={quote(str(old.relative_to(materials)))}">{esc(v.get("saved_at"))}</a></li>')
            content.append('</ul></details>')
        content.append('</div>')
    content.append('</section></main></html>')
    return ''.join(content)


def make_handler(materials: Path, indexing: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/file":
                relative = parse_qs(url.query).get("path", [""])[0]
                allowed = {str(p.relative_to(materials)) for r in load_rows(materials)
                           if (p := safe_file(materials, r.get("local_file", "")))}
                allowed.update(str(p.relative_to(materials)) for v in versions_for(materials)
                               if (p := safe_file(materials, v.get("local_file", ""))))
                path = safe_file(materials, relative) if relative in allowed else None
                if not path:
                    self.send_error(404)
                    return
                data = path.read_bytes()
                mime = "application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Disposition", "inline" if mime == "application/pdf" else "attachment")
            elif url.path == "/":
                data = render(materials, parse_qs(url.query), indexing).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            else:
                self.send_error(404)
                return
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Browse local Moodle study materials")
    parser.add_argument("--materials", type=Path, default=ROOT / "materials")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    materials = args.materials.resolve()
    materials.mkdir(parents=True, exist_ok=True)
    indexing = {"running": True}

    def build_index():
        try:
            stats = index_pdfs(materials)
            indexing["stats"] = stats
            print(f"PDF search ready: {stats['indexed']} indexed, {stats['unchanged']} unchanged, {stats['errors']} errors", flush=True)
        except Exception as exc:
            indexing["error"] = f"Indexing failed: {type(exc).__name__}: {exc}"
        finally:
            indexing["running"] = False

    threading.Thread(target=build_index, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(materials, indexing))
    print(f"Open http://127.0.0.1:{args.port} in your browser (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
