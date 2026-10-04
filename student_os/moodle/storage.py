"""Local file naming. Atomic writes, hashing and versioning are added in step 3."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

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


def safe_name(value: str) -> str:
    """Make a cross-platform, bounded path component."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    if value.upper() in WINDOWS_RESERVED:
        value = f"_{value}"
    return value[:MAX_NAME].rstrip(" .") or "Untitled"


def file_name(title: str, url: str, content_type: str) -> str:
    """Build ``<safe title>-<8 hex of sha256(url)><ext>`` for a downloaded file.

    The digest is of the resolved pluginfile URL. Step 6 relies on this to
    recognise files downloaded by the original script without re-fetching them.
    """
    path_name = unquote(Path(urlparse(url).path).name)
    suffix = Path(path_name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = MIME_EXT.get(content_type, mimetypes.guess_extension(content_type) or "")
    stem = safe_name(title)
    if suffix and stem.lower().endswith(suffix):
        stem = stem[: -len(suffix)]
    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    return f"{stem}-{digest}{suffix}"
