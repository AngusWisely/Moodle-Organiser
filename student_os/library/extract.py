"""Read the text out of course files: PDF, PowerPoint (.pptx), Word (.docx) and plain text.

Each file becomes a list of :class:`Page` objects (a PDF page, a slide, or a
Word section under a heading), each with a best-guess title. The titles make
the file's outline. Nothing here touches the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SUPPORTED = {".pdf", ".pptx", ".docx", ".txt", ".md"}
MAX_PAGES = 400              # huge reference books are only read this far
MAX_TITLE = 120
_DOCX_CHUNK = 40             # paragraphs per section when a Word file has no headings


class UnsupportedFile(Exception):
    """The file type can't be read (e.g. old .ppt/.doc, images, videos)."""


@dataclass(frozen=True)
class Page:
    number: int              # 1-based page, slide or section number
    title: str
    text: str


@dataclass(frozen=True)
class Document:
    kind: str                # pdf | pptx | docx | text
    pages: list[Page]
    truncated: bool = False  # True if MAX_PAGES was reached

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    @property
    def word_count(self) -> int:
        return sum(len(p.text.split()) for p in self.pages)

    def outline(self) -> list[tuple[int, str]]:
        """``(page, title)`` for each new title, skipping repeats like "Continued"."""
        result: list[tuple[int, str]] = []
        for page in self.pages:
            title = page.title.strip()
            if title and (not result or result[-1][1].lower() != title.lower()):
                result.append((page.number, title))
        return result


def extract(path: Path) -> Document:
    """Read a supported file. Raises UnsupportedFile for anything else."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf(path)
    if suffix == ".pptx":
        return _pptx(path)
    if suffix == ".docx":
        return _docx(path)
    if suffix in (".txt", ".md"):
        text = path.read_text(encoding="utf-8", errors="replace")
        return Document("text", [Page(1, _first_line(text), _clean(text))])
    raise UnsupportedFile(f"Can't read {suffix or 'files without an extension'}")


# --- formats -----------------------------------------------------------------------

def _pdf(path: Path) -> Document:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise UnsupportedFile("Password-protected PDF") from exc
    pages = []
    for number, page in enumerate(reader.pages[:MAX_PAGES], 1):
        try:
            text = _clean(page.extract_text() or "")
        except Exception:
            text = ""
        pages.append(Page(number, _first_line(text), text))
    return Document("pdf", pages, truncated=len(reader.pages) > MAX_PAGES)


def _pptx(path: Path) -> Document:
    from pptx import Presentation

    deck = Presentation(str(path))
    pages = []
    for number, slide in enumerate(deck.slides, 1):
        if number > MAX_PAGES:
            break
        title = ""
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text
        parts = [text for shape in slide.shapes for text in _shape_text(shape)]
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            parts.append(slide.notes_slide.notes_text_frame.text)
        text = _clean("\n".join(parts))
        pages.append(Page(number, _clip(title) or _first_line(text), text))
    return Document("pptx", pages, truncated=len(deck.slides) > MAX_PAGES)


def _shape_text(shape) -> list[str]:
    texts = []
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        texts.append(shape.text_frame.text)
    if getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            texts.append(" | ".join(cell.text for cell in row.cells))
    for child in getattr(shape, "shapes", []):  # grouped shapes
        texts.extend(_shape_text(child))
    return texts


def _docx(path: Path) -> Document:
    import docx

    document = docx.Document(str(path))
    sections: list[tuple[str, list[str]]] = []
    current_title, current = "", []
    has_headings = any(p.style is not None and p.style.name.lower().startswith(("heading", "title"))
                       for p in document.paragraphs)
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        is_heading = paragraph.style is not None and paragraph.style.name.lower().startswith(("heading", "title"))
        if has_headings and is_heading and text:
            if current or current_title:
                sections.append((current_title, current))
            current_title, current = text, []
            continue
        if text:
            current.append(text)
        if not has_headings and len(current) >= _DOCX_CHUNK:
            sections.append((current_title, current))
            current_title, current = "", []
    for table in document.tables:
        current.extend(" | ".join(cell.text for cell in row.cells) for row in table.rows)
    if current or current_title:
        sections.append((current_title, current))
    pages = []
    for number, (title, lines) in enumerate(sections[:MAX_PAGES], 1):
        text = _clean("\n".join(lines))
        pages.append(Page(number, _clip(title) or _first_line(text), text))
    return Document("docx", pages, truncated=len(sections) > MAX_PAGES)


# --- text helpers --------------------------------------------------------------------

def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _clip(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip()
    return title[:MAX_TITLE].rstrip()


def _first_line(text: str) -> str:
    """A plausible title: the first line with real words, not a page number or footer."""
    for line in text.splitlines()[:8]:
        line = line.strip()
        letters = sum(ch.isalpha() for ch in line)
        if 3 <= len(line) <= MAX_TITLE and letters >= 3 and letters / len(line) > 0.5:
            return _clip(line)
    return ""
