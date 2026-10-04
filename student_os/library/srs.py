"""Spaced repetition: when to see a flashcard next, from how well you knew it.

A simplified SM-2 (the scheme behind Anki):

* again: forgot it. See it again today; it becomes harder (lower ease).
* hard:  knew it with effort. A slightly longer gap; a little harder.
* good:  knew it. 1 day, then 3 days, then the gap grows by the ease (about 2.5x).
* easy:  knew it instantly. A longer gap than good; a little easier.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta

GRADES = ("again", "hard", "good", "easy")
MIN_EASE = 1.3
MAX_INTERVAL = 365.0


@dataclass(frozen=True)
class CardState:
    interval_days: float = 0.0
    ease: float = 2.5
    reps: int = 0              # successful reviews in a row
    lapses: int = 0
    due_on: date | None = None


def review(state: CardState, grade: str, today: date) -> CardState:
    """The card's new schedule after answering it with ``grade``."""
    if grade not in GRADES:
        raise ValueError(f"grade must be one of {GRADES}")
    if grade == "again":
        return replace(state, interval_days=0.0, ease=max(MIN_EASE, state.ease - 0.2), reps=0,
                       lapses=state.lapses + 1, due_on=today)
    if grade == "hard":
        interval = max(1.0, state.interval_days * 1.2)
        ease = max(MIN_EASE, state.ease - 0.15)
    else:
        if state.reps == 0:
            interval = 1.0
        elif state.reps == 1:
            interval = 3.0
        else:
            interval = state.interval_days * state.ease
        ease = state.ease
        if grade == "easy":
            interval = max(4.0, interval * 1.3)
            ease = state.ease + 0.15
    interval = min(MAX_INTERVAL, round(interval, 1))
    return replace(state, interval_days=interval, ease=round(ease, 2), reps=state.reps + 1,
                   due_on=today + timedelta(days=round(interval)))
