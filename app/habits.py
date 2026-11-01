"""How the student actually starts, from the `pings` table: per weekday, how late starts come,
and the "you usually start 25 min late on Tuesdays" line.

A try at starting opens with the first ping for an assignment after its last start, and closes
at the next start. How late it was is that start minus the first ping, so a missed start that got
moved to the evening counts from the time first planned. It belongs to the first ping's weekday,
in the student's zone. Pure functions over pings and blocks; main.py feeds them from the DB.
"""

from datetime import datetime
from statistics import median
from typing import NamedTuple

from app.clock import as_local
from app.models import Block, Ping

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MIN_STARTS = 3  # starts on a weekday before it says anything about that day
MIN_LATE = 10  # minutes: a median under this is on time


class Start(NamedTuple):
    planned: datetime  # the first ping of the try
    late: float  # minutes from that ping to the start, never below 0


def starts(pings: list[Ping], blocks: list[Block]) -> list[Start]:
    """Every start, with when it was first planned. Pings of deleted blocks are skipped."""
    owner = {b.id: b.assignment_id for b in blocks}
    first: dict[int, datetime] = {}  # assignment id -> first ping of its open try
    out = []
    for p in sorted(pings, key=lambda p: p.sent_at):
        a = owner.get(p.block_id)
        if a is None:
            continue
        opened = first.setdefault(a, p.sent_at)
        if p.started_at:
            out.append(Start(opened, max(0.0, (p.started_at - opened).total_seconds() / 60)))
            del first[a]
    return out


def _by_day(all_starts: list[Start]) -> list[list[float]]:
    late: list[list[float]] = [[] for _ in DAYS]
    for s in all_starts:
        late[as_local(s.planned).weekday()].append(s.late)
    return late


def late_text(minutes: float) -> str:
    """'25 min', 'about 3 hours', 'about 2 days'."""
    if minutes < 90:
        return f"{round(minutes)} min"
    if minutes < 36 * 60:
        return f"about {round(minutes / 60)} hours"
    return f"about {round(minutes / (24 * 60))} days"


def _line(day: int, late: list[float]) -> str | None:
    if len(late) < MIN_STARTS or median(late) < MIN_LATE:
        return None
    return f"You usually start {late_text(median(late))} late on {DAYS[day]}s."


def line(all_starts: list[Start], weekday: int) -> str | None:
    """'You usually start 25 min late on Tuesdays.' once a weekday has MIN_STARTS starts whose
    median is at least MIN_LATE minutes late; else None."""
    return _line(weekday, _by_day(all_starts)[weekday])


def lines(all_starts: list[Start]) -> list[str]:
    """Every weekday's line, the latest day first."""
    by_day = _by_day(all_starts)
    found = [(median(late), text) for day, late in enumerate(by_day) if (text := _line(day, late))]
    return [text for _, text in sorted(found, reverse=True)]


def weekdays(all_starts: list[Start], blocks: list[Block]) -> list[dict]:
    """One row per weekday, Monday first: starts, their median minutes late, and missed blocks."""
    missed = [0] * len(DAYS)
    for b in blocks:
        if b.status == "missed":
            missed[as_local(b.start).weekday()] += 1
    return [
        {"day": DAYS[i], "starts": len(late), "median_late": round(median(late)) if late else None, "missed": missed[i]}
        for i, late in enumerate(_by_day(all_starts))
    ]
