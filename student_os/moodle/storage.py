"""Local file storage: naming, atomic writes, SHA-256 hashing and versioning.

Downloads go to ``<materials>/.incoming/`` first and are hashed as they are
written. Only after the detector has classified the result is the temporary
file moved into place with :func:`os.replace`, so an interrupted run can never
leave a truncated file where a good copy is expected.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from .detector import Decision

MIME_EXT = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/msword": ".doc",
}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}
MAX_NAME = 95
INCOMING = ".incoming"
VERSIONS = "versions"
CHUNK = 1024 * 1024


# --- naming -------------------------------------------------------------------

def safe_name(value: str) -> str:
    """Make a cross-platform, bounded path component."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    if value.upper() in WINDOWS_RESERVED:
        value = f"_{value}"
    return value[:MAX_NAME].rstrip(" .") or "Untitled"


def file_name(title: str, url: str, content_type: str) -> str:
    """Legacy organiser.py name: ``<safe title>-<8 hex of sha256(url)><ext>``.

    The digest is of the resolved pluginfile URL. The detector relies on this
    to recognise files downloaded by the original script without re-fetching.
    """
    return _named(title, hashlib.sha256(url.encode()).hexdigest()[:8], _suffix(url, content_type))


def file_name_for_key(title: str, key: str, url: str, content_type: str) -> str:
    """Name for files first downloaded by the sync: digest of the stable resource key.

    Unlike :func:`file_name`, a new Moodle revision keeps the same name, so an
    updated file replaces its current copy in place.
    """
    return _named(title, hashlib.sha256(key.encode()).hexdigest()[:8], _suffix(url, content_type))


def _suffix(url: str, content_type: str) -> str:
    suffix = Path(unquote(Path(urlparse(url).path).name)).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    return MIME_EXT.get(content_type, mimetypes.guess_extension(content_type) or "")


def _named(title: str, digest: str, suffix: str) -> str:
    stem = safe_name(title)
    if suffix and stem.lower().endswith(suffix):
        stem = stem[: -len(suffix)]
    return f"{stem}-{digest}{suffix}"


def resolve_local(root: Path, relative: str) -> Path:
    """Resolve a stored relative path, refusing anything outside ``root``."""
    if Path(relative).is_absolute():
        raise ValueError(f"Stored path must be relative: {relative}")
    base = root.resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Stored path escapes {base}: {relative}")
    return path


# --- hashing and incoming downloads ------------------------------------------

@dataclass(frozen=True)
class IncomingFile:
    """A complete download waiting to be kept or discarded."""

    path: Path
    sha256: str
    size: int


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def write_incoming(materials: Path, body: bytes | Iterable[bytes]) -> IncomingFile:
    """Write a download to ``<materials>/.incoming/``, hashing as it goes.

    ``body`` may be bytes or an iterable of chunks (for future streaming). If
    writing fails part-way, the partial file is removed and the error re-raised.
    """
    folder = materials / INCOMING
    folder.mkdir(parents=True, exist_ok=True)
    chunks = (body,) if isinstance(body, bytes) else body
    digest, size = hashlib.sha256(), 0
    handle = tempfile.NamedTemporaryFile(dir=folder, suffix=".part", delete=False)
    path = Path(handle.name)
    try:
        with handle:
            for chunk in chunks:
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return IncomingFile(path, digest.hexdigest(), size)


def clean_incoming(materials: Path) -> int:
    """Delete leftovers from interrupted runs. Returns how many were removed."""
    folder = materials / INCOMING
    if not folder.is_dir():
        return 0
    removed = 0
    for leftover in folder.glob("*.part"):
        leftover.unlink(missing_ok=True)
        removed += 1
    return removed


# --- versioning and applying decisions ----------------------------------------

def archive(current: Path, when: datetime) -> Path:
    """Move ``current`` to ``versions/<stem>.<UTC timestamp><ext>`` beside it."""
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = current.parent / VERSIONS
    folder.mkdir(exist_ok=True)
    target = folder / f"{current.stem}.{stamp}{current.suffix}"
    counter = 2
    while target.exists():
        target = folder / f"{current.stem}.{stamp}-{counter}{current.suffix}"
        counter += 1
    os.replace(current, target)
    return target


def apply_decision(decision: Decision, incoming: Path | None, target: Path, when: datetime) -> Path | None:
    """Carry out a detector decision on disk. Returns the archived path, if any.

    Archives the current copy when asked, then either moves the download into
    place or discards it. A failed decision leaves everything untouched.
    """
    if decision.keep_new_file and incoming is None:
        raise ValueError("Decision keeps a new file but there is no download")
    archived = None
    if decision.archive_previous and target.is_file():
        archived = archive(target, when)
    if incoming is not None:
        if decision.keep_new_file:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(incoming, target)
        else:
            incoming.unlink(missing_ok=True)
    return archived
