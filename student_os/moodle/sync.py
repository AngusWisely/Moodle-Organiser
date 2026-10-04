"""Sync orchestration: discovered course items -> probe/fetch -> detector -> storage + database.

Browser-free: callers pass a :class:`~.session.CourseScan` and a
:class:`~.fetch.Transport`, so the whole flow is tested against a fake Moodle.

Each resource is handled on its own: a failure is recorded and the run moves
on, except an expired session, which stops the run (raised as AuthExpired).
Removals are only recorded after a complete, error-free scan of a module.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from .detector import (
    Decision,
    Failed,
    Fetched,
    FetchResult,
    Known,
    LocalFile,
    NotModified,
    Outcome,
    Plan,
    Remote,
    classify,
    plan,
)
from .fetch import (
    AuthExpired,
    Downloaded,
    FetchError,
    Transport,
    Unmodified,
    fetch_file,
    fetch_page,
    is_video_url,
    probe_resource,
)
from .models import Item
from .scraper import files_from_html
from .session import CourseScan
from .storage import IncomingFile, apply_decision, file_name_for_key, resolve_local, safe_name, write_incoming
from .urls import resource_key


@dataclass(frozen=True)
class SyncOptions:
    refresh: bool = False      # download and hash everything (except videos without --videos)
    videos: bool = False       # download videos rather than linking them
    dry_run: bool = False      # probe only: no downloads, no file changes


@dataclass
class ModuleReport:
    name: str
    counts: dict[Outcome, int] = field(default_factory=lambda: {o: 0 for o in Outcome})
    to_fetch: int = 0          # dry run: files that a real run would request
    removed: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return sum(self.counts.values()) + self.to_fetch

    def add(self, outcome: Outcome) -> None:
        self.counts[outcome] += 1


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Syncer:
    """Runs one sync across modules, writing to ``conn``, ``materials/`` and the event log."""

    def __init__(self, conn: sqlite3.Connection, transport: Transport, *, root: Path, materials: Path,
                 run_id: int, options: SyncOptions = SyncOptions(), clock: Clock = _utc_now) -> None:
        self.conn = conn
        self.transport = transport
        self.root = root
        self.materials = materials
        self.run_id = run_id
        self.options = options
        self.clock = clock

    # --- modules ---------------------------------------------------------------

    def sync_module(self, module: db.Module, scan: CourseScan) -> ModuleReport:
        """Sync one course. Raises AuthExpired if the session ends part-way."""
        report = ModuleReport(module.name, problems=list(scan.problems))
        started = self._now()
        complete = scan.complete
        for item in scan.items:
            try:
                if item.type == "folder":
                    complete &= self._sync_folder(module, item, report)
                else:
                    self._sync_item(module, item, report)
            except AuthExpired:
                raise
            except Exception as exc:  # one bad item must not stop the run
                report.add(Outcome.FAILED)
                report.problems.append(f"{item.title}: {type(exc).__name__}: {exc}")
        if complete and scan.items:
            with self.conn:
                gone = db.mark_unseen_removed(self.conn, module.id, started)
                for resource in gone:
                    db.log_event(self.conn, self.run_id, resource.id, "removed", self._now())
            report.removed = len(gone)
        return report

    # --- items -----------------------------------------------------------------

    def _sync_item(self, module: db.Module, item: Item, report: ModuleReport) -> None:
        resource = self._see(module, item, item.url, item.type)
        if item.type == "resource":
            probe = probe_resource(self.transport, item.url)
            if probe.file_url is None and probe.direct is None:
                self._linked(resource, report)  # a page with no file behind it
                return
            remote = Remote(probe.file_url, is_video=probe.is_video)
            self._sync_file(module, resource, remote, report, direct=probe.direct)
        elif item.type == "file":
            self._sync_file(module, resource, Remote(item.url, is_video=is_video_url(item.url)), report)
        else:
            self._linked(resource, report)  # assignments, pages, external links, quizzes...

    def _sync_folder(self, module: db.Module, item: Item, report: ModuleReport) -> bool:
        """Sync each file in a folder. Returns False if the folder could not be read."""
        container = self._see(module, item, item.url, "folder")
        try:
            page_url, html = fetch_page(self.transport, item.url)
        except AuthExpired:
            raise
        except FetchError as exc:
            report.problems.append(f"{item.title}: folder unavailable ({exc})")
            return False
        with self.conn:
            db.save_status(self.conn, container.id, db.LINKED)
        for file_url, name in files_from_html(html, page_url):
            file_item = Item(item.module, item.section, name, "folder_file", file_url)
            try:
                resource = self._see(module, file_item, file_url, "folder_file")
                self._sync_file(module, resource, Remote(file_url, is_video=is_video_url(file_url)), report)
            except AuthExpired:
                raise
            except Exception as exc:
                report.add(Outcome.FAILED)
                report.problems.append(f"{item.title}/{name}: {type(exc).__name__}: {exc}")
        return True

    def _see(self, module: db.Module, item: Item, url: str, resource_type: str) -> db.Resource:
        with self.conn:
            resource, reappeared = db.see_resource(
                self.conn, key=resource_key(url), module_id=module.id, section=item.section, title=item.title,
                resource_type=resource_type, source_url=url, at=self._now())
            if reappeared:
                db.log_event(self.conn, self.run_id, resource.id, "reappeared", self._now())
        return resource

    def _linked(self, resource: db.Resource, report: ModuleReport, status: str = db.LINKED) -> None:
        with self.conn:
            db.save_status(self.conn, resource.id, status)
        report.add(Outcome.LINKED)

    # --- files -----------------------------------------------------------------

    def _sync_file(self, module: db.Module, resource: db.Resource, remote: Remote, report: ModuleReport, *,
                   direct: Downloaded | None = None) -> None:
        known = Known(resource.content_hash, resource.file_size, resource.file_url, resource.etag,
                      resource.last_modified, resource.legacy_url_digest)
        current = self._local_path(resource)
        local = LocalFile.from_path(current)
        action = plan(known, local, remote, refresh=self.options.refresh, videos=self.options.videos)
        if direct is not None and action is not Plan.LINK_VIDEO:
            action = Plan.DOWNLOAD  # the body is already in hand; hashing it costs nothing

        if action is Plan.SKIP:
            with self.conn:
                db.save_unchanged(self.conn, resource.id, file_url=remote.file_url)
            report.add(Outcome.UNCHANGED)
            return
        if action is Plan.LINK_VIDEO:
            self._linked(resource, report, db.VIDEO_LINK)
            return
        if self.options.dry_run:
            report.to_fetch += 1
            return

        url = remote.file_url or resource.source_url
        response = self._fetch(url, known, conditional=action is Plan.CONDITIONAL_GET, direct=direct)
        incoming = write_incoming(self.materials, response.body) if isinstance(response, Downloaded) else None
        try:
            decision = classify(known, local, self._result(response, incoming))
            if decision.outcome is Outcome.FAILED:
                self._record_failure(resource, decision, report)
                return
            if isinstance(response, Downloaded) and incoming is not None:
                target = current or self._new_path(module, resource, response)
                archived = apply_decision(decision, incoming.path, target, self.clock())
                self._record_file(resource, decision, response, incoming.sha256, incoming.size, target, archived)
            else:
                assert isinstance(response, Unmodified)
                with self.conn:
                    db.save_unchanged(self.conn, resource.id, etag=response.etag,
                                      last_modified=response.last_modified)
        finally:
            if incoming is not None:
                incoming.path.unlink(missing_ok=True)  # already moved into place unless discarded
        report.add(decision.outcome)

    def _fetch(self, url: str, known: Known, *, conditional: bool,
               direct: Downloaded | None) -> Downloaded | Unmodified | Failed:
        if direct is not None:
            return direct
        try:
            return fetch_file(self.transport, url, etag=known.etag, last_modified=known.last_modified,
                              conditional=conditional)
        except AuthExpired as exc:
            return Failed(str(exc), auth=True)
        except FetchError as exc:
            return Failed(str(exc))

    @staticmethod
    def _result(response: Downloaded | Unmodified | Failed, incoming: IncomingFile | None) -> FetchResult:
        if isinstance(response, Failed):
            return response
        if isinstance(response, Unmodified):
            return NotModified()
        assert incoming is not None
        return Fetched(incoming.sha256, incoming.size)

    def _record_failure(self, resource: db.Resource, decision: Decision, report: ModuleReport) -> None:
        with self.conn:
            db.save_failure(self.conn, resource.id, decision.reason)
            db.log_event(self.conn, self.run_id, resource.id, "failed", self._now(), message=decision.reason)
        report.add(Outcome.FAILED)
        report.problems.append(f"{resource.title}: {decision.reason}")
        if decision.auth_failure:
            raise AuthExpired(decision.reason)

    def _record_file(self, resource: db.Resource, decision: Decision, response: Downloaded, sha256: str,
                     size: int, target: Path, archived: Path | None) -> None:
        at = self._now()
        with self.conn:
            if decision.outcome is Outcome.UNCHANGED:
                db.save_unchanged(self.conn, resource.id, file_url=response.url, etag=response.etag,
                                  last_modified=response.last_modified)
                return
            db.save_download(self.conn, resource.id, file_url=response.url, etag=response.etag,
                             last_modified=response.last_modified, local_path=self._relative(target),
                             file_size=size, content_hash=sha256, at=at,
                             changed=decision.outcome in (Outcome.NEW, Outcome.UPDATED))
            db.log_event(self.conn, self.run_id, resource.id, decision.outcome.value, at,
                         old_hash=resource.content_hash, new_hash=sha256,
                         archived_path=self._relative(archived) if archived else None)

    # --- paths -----------------------------------------------------------------

    def _local_path(self, resource: db.Resource) -> Path | None:
        if not resource.local_path:
            return None
        try:
            return resolve_local(self.root, resource.local_path)
        except ValueError:
            return None

    def _new_path(self, module: db.Module, resource: db.Resource, response: Downloaded) -> Path:
        name = file_name_for_key(resource.title, resource.key, response.url, response.content_type)
        return self.materials / safe_name(module.name) / safe_name(resource.section or "General") / name

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def _now(self) -> str:
        return _stamp(self.clock())
