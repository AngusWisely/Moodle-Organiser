"""Plain data types shared by the Moodle scraper and sync code."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    """A link found on a Moodle course page, plus its last known outcome.

    ``type`` is the Moodle activity kind taken from ``/mod/<kind>/view.php``
    (e.g. ``resource``, ``folder``, ``assign``) or ``file`` for a direct
    ``pluginfile.php`` link.
    """

    module: str
    section: str
    title: str
    type: str
    url: str
    status: str = "linked"
    local_file: str = ""
