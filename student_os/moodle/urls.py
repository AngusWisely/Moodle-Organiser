"""Moodle base URLs and the host allow-list.

Every URL the tool requests or follows must pass :func:`allowed`, so session
cookies are only ever sent to Nottingham Moodle.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

BASE = "https://moodle.nottingham.ac.uk"
COURSES_URL = f"{BASE}/my/courses.php"
HOST = "moodle.nottingham.ac.uk"

# pluginfile.php/<contextid>/<component>/content/<revision>/<path...>
# Moodle bumps <revision> when an activity's files are replaced; it exists only
# to defeat caching, so the same file at a new revision is the same resource.
_REVISIONED = re.compile(r"^/pluginfile\.php/\d+/(mod_resource|mod_folder|mod_page)/content/(\d+)/")


def allowed(url: str) -> bool:
    """Return True only for HTTPS URLs on the exact Nottingham Moodle host."""
    parts = urlparse(url)
    return parts.scheme == "https" and parts.hostname == HOST


def file_revision(url: str) -> int | None:
    """Return the Moodle file revision in a pluginfile URL, or None if it has none."""
    if not allowed(url):
        return None
    match = _REVISIONED.match(urlparse(url).path)
    return int(match.group(2)) if match else None


def resource_key(url: str) -> str:
    """Stable identity for a Moodle link, unaffected by file revisions or renames of the module.

    * ``/mod/<kind>/view.php?id=N`` -> ``cm:N`` (course-module id)
    * ``/pluginfile.php/<ctx>/<component>/<area>/[<rev>/]<path>`` -> ``file:<ctx>/<component>/<area>/<path>``
    * anything else -> ``url:<url without fragment>``
    """
    parts = urlparse(url)
    if re.fullmatch(r"/mod/[a-z]+/view\.php", parts.path):
        ids = [v for k, v in parse_qsl(parts.query) if k == "id"]
        if ids and ids[0].isdigit():
            return f"cm:{ids[0]}"
    if parts.path.startswith("/pluginfile.php/"):
        rest = parts.path[len("/pluginfile.php/"):]
        match = _REVISIONED.match(parts.path)
        if match:
            ctx = rest.split("/", 1)[0]
            rest = f"{ctx}/{match.group(1)}/content/{parts.path[match.end():]}"
        return f"file:{unquote(rest)}"
    return f"url:{urlunparse(parts._replace(fragment=''))}"


def cmid(url: str) -> int | None:
    """Course-module id from a ``/mod/<kind>/view.php?id=N`` URL."""
    key = resource_key(url)
    return int(key[3:]) if key.startswith("cm:") else None


def comparable_file_url(url: str) -> str:
    """Normalise a file URL for equality checks: drop the fragment and ``forcedownload``."""
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "forcedownload"]
    return urlunparse(parts._replace(query=urlencode(query), fragment=""))
