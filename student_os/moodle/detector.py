"""Decide whether a Moodle file needs fetching, and what a fetch result means.

Pure logic: no network, database or file writes, so every rule is unit-tested.

Sync calls :func:`plan` first. If the plan is a download or conditional GET it
performs the request (writing any body to a temporary file and hashing it),
then calls :func:`classify` with the result to learn the outcome and whether
to keep the new file and archive the old one.

Principle: Moodle metadata (file revision, ETag, Last-Modified) only decides
whether to *look*. Only a SHA-256 mismatch proves the content *changed*.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .urls import comparable_file_url, file_revision


class Plan(StrEnum):
    """What to request for one file."""

    SKIP = "skip"                        # trust the stored copy; no request
    LINK_VIDEO = "link_video"            # record as a link only
    CONDITIONAL_GET = "conditional_get"  # If-None-Match / If-Modified-Since
    DOWNLOAD = "download"                # unconditional GET, then hash


class Outcome(StrEnum):
    """Result of syncing one resource, as reported and logged."""

    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    RESTORED = "restored"
    FAILED = "failed"
    LINKED = "linked"


@dataclass(frozen=True)
class Known:
    """What the database remembers about a file from earlier runs."""

    content_hash: str | None             # None until a download has succeeded
    file_size: int | None
    file_url: str | None = None          # resolved pluginfile URL, including revision
    etag: str | None = None
    last_modified: str | None = None
    legacy_url_digest: str | None = None  # from organiser.py filenames, for migration

    @property
    def downloaded(self) -> bool:
        return self.content_hash is not None


@dataclass(frozen=True)
class LocalFile:
    """The current local copy, as seen on disk."""

    exists: bool
    size: int | None = None

    @classmethod
    def from_path(cls, path: Path | None) -> LocalFile:
        if path is None or not path.is_file():
            return cls(exists=False)
        return cls(exists=True, size=path.stat().st_size)


@dataclass(frozen=True)
class Remote:
    """What is known about the file on Moodle before downloading it."""

    file_url: str | None                 # from the page or the view.php redirect
    is_video: bool = False


@dataclass(frozen=True)
class NotModified:
    """The server answered 304 to a conditional GET."""


@dataclass(frozen=True)
class Fetched:
    """A body was downloaded to a temporary file and hashed."""

    content_hash: str
    size: int


@dataclass(frozen=True)
class Failed:
    """The request failed; ``auth`` means the Moodle session has expired."""

    reason: str
    auth: bool = False


FetchResult = NotModified | Fetched | Failed


@dataclass(frozen=True)
class Decision:
    """How sync should act on a fetch result."""

    outcome: Outcome
    keep_new_file: bool = False          # move the temporary download into place
    archive_previous: bool = False       # move the current local file to versions/ first
    reason: str = ""
    auth_failure: bool = False


def intact(known: Known, local: LocalFile) -> bool:
    """True if the local copy exists and matches the size recorded at download."""
    return local.exists and (known.file_size is None or local.size == known.file_size)


def legacy_digest(url: str) -> str:
    """The 8-character digest organiser.py put in filenames: sha256(resolved URL)."""
    return hashlib.sha256(url.encode()).hexdigest()[:8]


def plan(known: Known | None, local: LocalFile, remote: Remote, *, refresh: bool = False,
         videos: bool = False) -> Plan:
    """Choose the cheapest request that can still notice a changed file."""
    have_copy = known is not None and known.downloaded and intact(known, local)

    if remote.is_video and not videos:
        return Plan.SKIP if have_copy else Plan.LINK_VIDEO
    if not have_copy or refresh:
        return Plan.DOWNLOAD
    assert known is not None  # have_copy implies a record

    url = remote.file_url
    if url and _same_revision(known, url):
        return Plan.SKIP
    if known.etag or known.last_modified:
        return Plan.CONDITIONAL_GET
    if url and known.file_url and file_revision(url) is None and _same_url(url, known.file_url):
        return Plan.SKIP  # unversioned and no validators: only --refresh can check it
    return Plan.DOWNLOAD


def classify(known: Known | None, local: LocalFile, result: FetchResult) -> Decision:
    """Turn a fetch result into an outcome. Failures never touch stored state."""
    if isinstance(result, Failed):
        return Decision(Outcome.FAILED, reason=result.reason, auth_failure=result.auth)
    if isinstance(result, NotModified):
        if known is None or not known.downloaded:
            return Decision(Outcome.FAILED, reason="304 without a stored copy")
        return Decision(Outcome.UNCHANGED)

    if known is None or not known.downloaded:
        return Decision(Outcome.NEW, keep_new_file=True)
    same_content = result.content_hash == known.content_hash
    if not intact(known, local):
        outcome = Outcome.RESTORED if same_content else Outcome.UPDATED
        return Decision(outcome, keep_new_file=True, archive_previous=local.exists)
    if same_content:
        return Decision(Outcome.UNCHANGED)
    return Decision(Outcome.UPDATED, keep_new_file=True, archive_previous=True)


def _same_revision(known: Known, url: str) -> bool:
    if known.file_url is None:
        return known.legacy_url_digest is not None and legacy_digest(url) == known.legacy_url_digest
    return file_revision(url) is not None and _same_url(url, known.file_url)


def _same_url(a: str, b: str) -> bool:
    return comparable_file_url(a) == comparable_file_url(b)
