"""Find dates in course text and flag the ones that look like deadlines.

UK conventions: numeric dates are day-first (12/11/2026 is 12 November).
Numeric dates without a year are ignored (too easily confused with ratios or
figure numbers). A written date without a year ("Friday 14th November") gets
the year that puts it closest to the academic year in progress.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

MONTHS = {name: number for number, names in enumerate([
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
    ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
    ("oct", "october"), ("nov", "november"), ("dec", "december")], 1) for name in names}
_MONTH = r"(?P<month>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<day>[0-3]?\d)(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>(?:19|20)\d{2})"
_WEEKDAY = r"(?:(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*,?\s+)?"

PATTERNS = [
    re.compile(rf"\b{_WEEKDAY}{_DAY}\s+(?:of\s+)?{_MONTH}(?:,?\s+{_YEAR})?\b", re.I),     # 14th November 2026
    re.compile(rf"\b{_WEEKDAY}{_MONTH}\s+{_DAY}(?:,?\s+{_YEAR})?\b", re.I),              # November 14, 2026
    re.compile(r"\b(?P<day>[0-3]?\d)[/.](?P<month>[01]?\d)[/.](?P<year>(?:20)?\d{2})\b"),  # 14/11/2026, 14.11.26
    re.compile(r"\b(?P<year>20\d{2})-(?P<month>[01]\d)-(?P<day>[0-3]\d)\b"),              # 2026-11-14
]
DEADLINE_WORDS = re.compile(
    r"\b(deadline|due|submit|submission|submitted|hand[- ]?in|closes?|closing|no later than|by\s+(?:noon|midday|\d))",
    re.I)
SNIPPET = 90


EXAM_WORDS = re.compile(r"\b(exams?|examination|class test|in-class test|assessment test|viva)\b", re.I)


@dataclass(frozen=True)
class DateMention:
    on: date
    snippet: str             # the sentence it appears in
    kind: str                # "deadline", "exam" or "date"

    @property
    def deadline(self) -> bool:
        return self.kind == "deadline"


def find_dates(text: str, *, today: date) -> list[DateMention]:
    """All dates in ``text``, once each per date and context, in order of appearance."""
    found: dict[tuple[date, str], tuple[int, DateMention]] = {}
    taken: list[tuple[int, int]] = []
    for pattern in PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in taken):
                continue  # already matched by a more specific pattern
            on = _to_date(match, today)
            if on is None:
                continue
            taken.append((match.start(), match.end()))
            snippet = _sentence(text, match.start(), match.end())
            mention = DateMention(on, snippet, _kind(snippet))
            found.setdefault((on, snippet), (match.start(), mention))
    return [mention for _, mention in sorted(found.values(), key=lambda item: item[0])]


def _kind(sentence: str) -> str:
    if DEADLINE_WORDS.search(sentence):
        return "deadline"
    if EXAM_WORDS.search(sentence):
        return "exam"
    return "date"


def _to_date(match: re.Match[str], today: date) -> date | None:
    month_text = match.group("month").lower().rstrip(".")
    month = MONTHS.get(month_text) if not month_text.isdigit() else int(month_text)
    day = int(match.group("day"))
    year_text = match.groupdict().get("year")
    if month is None:
        return None
    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    if match.re is PATTERNS[2]:
        return None  # numeric dates need a year
    return _guess_year(month, day, today)


def _guess_year(month: int, day: int, today: date) -> date | None:
    """Pick the year that puts the date between ~5 months ago and ~7 months ahead."""
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if today - timedelta(days=150) <= candidate < today + timedelta(days=215):
            return candidate
    return None


_SENTENCE_END = re.compile(r"[.!?](?=\s)|[;\n]")


def _sentence(text: str, start: int, end: int) -> str:
    """The sentence (or line) containing the date, trimmed to a readable length."""
    left = 0
    for boundary in _SENTENCE_END.finditer(text, max(0, start - 400), start):
        left = boundary.end()
    following = _SENTENCE_END.search(text, end)
    right = following.start() + 1 if following else len(text)
    sentence = re.sub(r"\s+", " ", text[left:right]).strip()
    if len(sentence) > 2 * SNIPPET:
        middle = len(re.sub(r"\s+", " ", text[left:start]).lstrip())
        lo = max(0, middle - SNIPPET)
        sentence = ("…" if lo else "") + sentence[lo:lo + 2 * SNIPPET].strip() + "…"
    return sentence
