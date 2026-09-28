"""Download accessible Nottingham Moodle course files and index the rest."""

from __future__ import annotations

import argparse
import csv
import hashlib
import mimetypes
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

BASE = "https://moodle.nottingham.ac.uk"
COURSES_URL = f"{BASE}/my/courses.php"
ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "materials"
PROFILE = ROOT / ".browser-profile"
FIELDS = ("module", "section", "title", "type", "url", "status", "local_file")
MIME_EXT = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/msword": ".doc",
}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


@dataclass(frozen=True)
class Item:
    module: str
    section: str
    title: str
    type: str
    url: str
    status: str = "linked"
    local_file: str = ""


def safe_name(value: str) -> str:
    """Make a cross-platform, bounded path component."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}:
        value = f"_{value}"
    return value[:95].rstrip(" .") or "Untitled"


def allowed(url: str) -> bool:
    parts = urlparse(url)
    return parts.scheme == "https" and parts.hostname == "moodle.nottingham.ac.uk"


def course_links(html: str, base: str = BASE) -> list[tuple[str, str]]:
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
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for a in soup.select('a[href*="/course/section.php"]'):
        url = urljoin(base, a.get("href", ""))
        if allowed(url) and url not in urls:
            urls.append(url)
    return urls[:100]


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


def file_name(title: str, url: str, content_type: str) -> str:
    path_name = unquote(Path(urlparse(url).path).name)
    suffix = Path(path_name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = MIME_EXT.get(content_type, mimetypes.guess_extension(content_type) or "")
    stem = safe_name(title)
    if suffix and stem.lower().endswith(suffix):
        stem = stem[: -len(suffix)]
    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    return f"{stem}-{digest}{suffix}"


def files_from_html(html: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.select('a[href*="pluginfile.php/"]'):
        url = urljoin(base, a.get("href", ""))
        if allowed(url):
            found[url] = a.get_text(" ", strip=True) or unquote(Path(urlparse(url).path).name)
    return list(found.items())


def local_get(context, url: str):
    """Follow redirects only while they remain on Nottingham Moodle."""
    for _ in range(8):
        if not allowed(url):
            raise ValueError("Redirect left Nottingham Moodle")
        response = context.request.get(url, timeout=15000, max_redirects=0)
        if response.status not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            raise ValueError("Redirect had no location")
        url = urljoin(url, location)
    raise ValueError("Too many redirects")


def fetch_file(
    context, item: Item, url: str, name: str,
    previous: dict[tuple[str, str], Item], *, videos: bool, refresh: bool,
) -> Item:
    """Save a Moodle-hosted file; record HTML pages without saving them."""
    cached = previous.get((item.module, url))
    is_video = Path(name.lower()).suffix in VIDEO_EXT
    if cached and cached.status == "downloaded" and cached.local_file and (ROOT / cached.local_file).is_file() and (not refresh or (is_video and not videos)):
        return cached
    if not videos and is_video:
        return Item(item.module, item.section, name, item.type, url, "linked video")
    try:
        response = local_get(context, url)
        if not response.ok or not allowed(response.url):
            return Item(item.module, item.section, name, item.type, url, "error: access denied or external redirect")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type.startswith("text/html"):
            return Item(item.module, item.section, name, item.type, url, "linked page")
        folder = OUTPUT / safe_name(item.module) / safe_name(item.section)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / file_name(name, response.url, content_type)
        if refresh or not target.exists():
            target.write_bytes(response.body())
        return Item(item.module, item.section, name, item.type, url, "downloaded", str(target.relative_to(ROOT)))
    except Exception as exc:
        return Item(item.module, item.section, name, item.type, url, f"error: {type(exc).__name__}")


def process(
    context, item: Item, previous: dict[tuple[str, str], Item],
    *, videos: bool, refresh: bool,
) -> list[Item]:
    if item.type in {"resource", "file"}:
        return [fetch_file(context, item, item.url, item.title, previous, videos=videos, refresh=refresh)]
    if item.type != "folder":
        return [item]
    try:
        response = local_get(context, item.url)
        if not response.ok or not allowed(response.url):
            return [Item(item.module, item.section, item.title, item.type, item.url, "error: folder unavailable")]
        file_urls = files_from_html(response.text(), response.url)
        if not file_urls:
            return [Item(item.module, item.section, item.title, item.type, item.url, "linked folder (no files found)")]
        results = []
        for n, (url, name) in enumerate(file_urls, 1):
            print(f"    Folder file {n}/{len(file_urls)}: {safe_name(name)[:65]}", flush=True)
            results.append(fetch_file(context, item, url, name, previous, videos=videos, refresh=refresh))
        return results
    except Exception as exc:
        return [Item(item.module, item.section, item.title, item.type, item.url, f"error: {type(exc).__name__}")]


def save_index(rows: list[Item]) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT / "index.csv"
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(asdict(item) for item in rows)


def read_index() -> dict[tuple[str, str], Item]:
    index = OUTPUT / "index.csv"
    if not index.exists():
        return {}
    with index.open(newline="", encoding="utf-8-sig") as handle:
        return {
            (row["module"], row["url"]): Item(**{key: row.get(key, "") for key in FIELDS})
            for row in csv.DictReader(handle)
            if row.get("module") and row.get("url")
        }


def choose_courses(courses: list[tuple[str, str]]) -> list[tuple[str, str]]:
    for n, (name, _) in enumerate(courses, 1):
        print(f"{n:2}. {name}")
    while True:
        answer = input("\nSelect module numbers (e.g. 1,2,4), or all: ").strip().lower()
        if answer == "all":
            return courses
        try:
            nums = sorted({int(n.strip()) for n in answer.split(",")})
            if nums and all(1 <= n <= len(courses) for n in nums):
                return [courses[n - 1] for n in nums]
        except ValueError:
            pass
        print("Please enter valid numbers from the list.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Organise Nottingham Moodle teaching materials")
    parser.add_argument("--videos", action="store_true", help="also download new video files")
    parser.add_argument("--refresh", action="store_true", help="re-download previously saved files if lecturers replaced them")
    args = parser.parse_args()
    print("Moodle organiser 3: weekly updates; new videos indexed as links", flush=True)
    OUTPUT.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            str(PROFILE), headless=False, accept_downloads=True
        )
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(COURSES_URL, wait_until="domcontentloaded", timeout=60000)
            print("Sign in to Moodle in the browser, including MFA if asked.")
            input("When you can see 'My Modules', press Enter here... ")
            page.goto(COURSES_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
            courses = course_links(page.content(), page.url)
            if not courses:
                print("No modules found. Check sign-in and that My Modules is set to show all.")
                return 1
            selected = choose_courses(courses)
            rows = read_index()
            previous = rows.copy()
            for module, url in selected:
                print(f"\nScanning {module}...")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1000)
                # A course can contain collapsed sections plus separate section pages.
                for button in page.get_by_text("Expand all", exact=True).all()[:2]:
                    try:
                        button.click(timeout=1500)
                    except Exception:
                        pass
                html = page.content()
                found = activities(html, module, page.url)
                for section_url in section_links(html, page.url):
                    try:
                        page.goto(section_url, wait_until="domcontentloaded", timeout=45000)
                        found.extend(activities(page.content(), module, page.url))
                    except Exception as exc:
                        print(f"  Could not scan section: {type(exc).__name__}")
                unique = {item.url: item for item in found}
                print(f"  Found {len(unique)} distinct items")
                for n, item in enumerate(unique.values(), 1):
                    print(f"  Item {n}/{len(unique)}: {safe_name(item.title)[:65]}", flush=True)
                    for result in process(browser, item, previous, videos=args.videos, refresh=args.refresh):
                        rows[(result.module, result.url)] = result
                        if result.status.startswith("error"):
                            print(f"    {result.status}", flush=True)
                    save_index(list(rows.values()))
                save_index(list(rows.values()))
            print(f"\nDone. Index: {OUTPUT / 'index.csv'}")
            print(f"Downloaded files indexed: {sum(row.status == 'downloaded' for row in rows.values())}")
            print(f"New items indexed this run: {sum(key not in previous for key in rows)}")
            print("Review index.csv for linked-only items and errors.")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Files already downloaded remain in materials/.")
        sys.exit(130)
