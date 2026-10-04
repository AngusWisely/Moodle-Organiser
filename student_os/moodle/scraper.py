"""Pure HTML parsing of Moodle pages. No network or browser access here."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .models import Item
from .urls import BASE, allowed

MAX_SECTION_PAGES = 100


def course_links(html: str, base: str = BASE) -> list[tuple[str, str]]:
    """Return ``(name, url)`` for each distinct course on a My Modules page."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for a in soup.select('a[href*="/course/view.php"]'):
        url = urljoin(base, a.get("href", ""))
        if allowed(url) and "id=" in urlparse(url).query:
            title = a.get_text(" ", strip=True)
            if title and title.lower() not in {"view course", "course"}:
                found.setdefault(url.split("#", 1)[0], title)
    return [(name, url) for url, name in found.items()]


def section_links(html: str, base: str) -> list[str]:
    """Return separate section-page URLs linked from a course page."""
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for a in soup.select('a[href*="/course/section.php"]'):
        url = urljoin(base, a.get("href", ""))
        if allowed(url) and url not in urls:
            urls.append(url)
    return urls[:MAX_SECTION_PAGES]


def activities(html: str, module: str, base: str) -> list[Item]:
    """Collect Moodle activity links, retaining the nearest section heading."""
    soup = BeautifulSoup(html, "html.parser")
    result: list[Item] = []
    seen: set[str] = set()
    sections = soup.select("li.section, section.course-section, [data-for='section']")
    if not sections:
        sections = [soup]
    for section in sections:
        heading = section.select_one(".sectionname, .section-title, h3, h2")
        section_name = heading.get_text(" ", strip=True) if heading else "General"
        blocks = section.select("li.activity, div.activity-item, div.activity")
        if not blocks:
            blocks = [section]
        for block in blocks:
            for a in block.select("a[href]"):
                url = urljoin(base, a["href"]).split("#", 1)[0]
                if not allowed(url) or url in seen:
                    continue
                path = urlparse(url).path
                match = re.search(r"/mod/([a-z]+)/view\.php", path)
                if not match and "/pluginfile.php/" not in path:
                    continue
                title_node = a.select_one(".instancename")
                title = (title_node or a).get_text(" ", strip=True)
                title = re.sub(r"\s*File\s*$", "", title).strip()
                if not title:
                    continue
                kind = match.group(1) if match else "file"
                result.append(Item(module, section_name, title, kind, url))
                seen.add(url)
    return result


def files_from_html(html: str, base: str) -> list[tuple[str, str]]:
    """Return ``(url, name)`` for each Moodle-hosted file linked on a page (e.g. a folder)."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for a in soup.select('a[href*="pluginfile.php/"]'):
        url = urljoin(base, a.get("href", ""))
        if allowed(url):
            found[url] = a.get_text(" ", strip=True) or unquote(Path(urlparse(url).path).name)
    return list(found.items())
