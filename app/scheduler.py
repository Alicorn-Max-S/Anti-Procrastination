"""Turns assignments into start-time blocks and decides when a ping should fire.

Pure logic over models.py types; no network, no DB (callers load/save with db.py). Works backwards
from due_at: heavier assignments get longer blocks that start more days ahead, blocks never
overlap, and they sit inside a daily window (WORK_START_HOUR to WORK_END_HOUR, local time). Only
when nothing fits before the due date does a block go outside the window or get flagged at_risk.
"""

from datetime import datetime, timedelta

from app.clock import local_tz
from app.config import settings
from app.models import Assignment, Block, PrepCard

SLOT = timedelta(minutes=15)
DURATION_MINUTES = {"light": 30, "medium": 60, "heavy": 90}
LEAD_DAYS = {"light": 1, "medium": 2, "heavy": 3}
ACTIVE = {"planned", "pinged", "started"}
MAX_LATE_PING = timedelta(hours=1)  # a planned block later than this is missed, not pinged
STALE_MISSED = timedelta(hours=3)  # unanswered missed blocks older than this get a fresh block

Window = tuple[int, int]
Busy = list[tuple[datetime, datetime]]


def default_window() -> Window:
    return (settings.work_start_hour, settings.work_end_hour)


def ceil_slot(dt: datetime) -> datetime:
    """Round up to the next quarter hour."""
    floored = dt.replace(minute=dt.minute - dt.minute % 15, second=0, microsecond=0)
    return floored if floored == dt else floored + SLOT


def overlapping(start: datetime, end: datetime, busy: Busy) -> Busy:
    return [(s, e) for s, e in busy if start < e and s < end]


def find_slot(
    start_from: datetime,
    length: timedelta,
    busy: Busy,
    deadline: datetime | None = None,
    window: Window | None = None,
    horizon: timedelta = timedelta(days=21),
) -> datetime | None:
    """Earliest quarter-hour start at or after `start_from` where `length` fits without overlapping
    `busy`, inside the daily `window` if given, ending by `deadline` if given."""
    tz = local_tz()
    t = ceil_slot(start_from.astimezone(tz))
    limit = t + horizon
    while t < limit:
        if deadline is not None and t + length > deadline:
            return None
        if window is not None:
            day_start = t.replace(hour=window[0], minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(hours=window[1] - window[0])
            if t < day_start:
                t = day_start
                continue
            if t + length > day_end:
                t = day_start + timedelta(days=1)
                continue
        clash = overlapping(t, t + length, busy)
        if clash:
            t = ceil_slot(max(e for _, e in clash).astimezone(tz))
            continue
        return t
    return None


def _is_live(b: Block, now: datetime) -> bool:
    """Active blocks, plus missed ones still waiting for a check-in answer."""
    return b.status in ACTIVE or (b.status == "missed" and now - b.end < STALE_MISSED)


def plan_blocks(
    assignments: list[Assignment],
    cards: list[PrepCard],
    now: datetime,
    existing: list[Block] | None = None,
    window: Window | None = None,
) -> list[Block]:
    """Propose a planned Block for every unfinished assignment due after `now` that doesn't already
    have a live or done block. Returns only the new blocks (without ids)."""
    window = window or default_window()
    effort = {c.assignment_id: c.effort for c in cards}
    busy: Busy = []
    covered: set[int] = set()
    for b in existing or []:
        if _is_live(b, now):
            busy.append((b.start, b.end))
        if _is_live(b, now) or b.status == "done":
            covered.add(b.assignment_id)

    todo = sorted(
        (a for a in assignments if not a.submitted and a.due_at and a.due_at > now and a.id not in covered),
        key=lambda a: a.due_at,
    )
    earliest = ceil_slot(now)
    tz = local_tz()
    new: list[Block] = []
    for a in todo:
        e = effort.get(a.id, "medium")
        length = timedelta(minutes=DURATION_MINUTES[e])
        lead_day = (a.due_at.astimezone(tz) - timedelta(days=LEAD_DAYS[e])).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start = (
            find_slot(max(earliest, lead_day), length, busy, a.due_at, window)
            or find_slot(earliest, length, busy, a.due_at, window)
            or find_slot(earliest, length, busy, a.due_at, None)  # outside the window, still in time
        )
        at_risk = start is None
        if start is None:
            start = find_slot(earliest, length, busy) or earliest
        block = Block(assignment_id=a.id, start=start, end=start + length, at_risk=at_risk)
        new.append(block)
        busy.append((block.start, block.end))
    return new


def due_ping(blocks: list[Block], now: datetime) -> Block | None:
    """The block whose start time has arrived and hasn't been pinged yet, if any."""
    due = [b for b in blocks if b.status == "planned" and b.start <= now < b.start + MAX_LATE_PING]
    return min(due, key=lambda b: b.start) if due else None


def undo_done(
    blocks: list[Block], assignment_id: int, now: datetime, started: set[int]
) -> tuple[list[Block], list[int]]:
    """Reverse "Mark done" for one assignment. Returns (changed blocks, ids of blocks to delete).

    The zero-length marker block "Mark done" adds when nothing was planned is deleted. Every other
    done block goes back to what it would be now: started if the student started it (`started`
    holds block ids with a recorded start), planned if it hasn't begun, otherwise missed (without
    a ping), which asks for a new time or gets replanned like any other miss."""
    changed: list[Block] = []
    deleted: list[int] = []
    for b in blocks:
        if b.assignment_id != assignment_id or b.status != "done":
            continue
        if b.start == b.end:
            deleted.append(b.id)
        elif b.id in started:
            changed.append(b.model_copy(update={"status": "started"}))
        elif b.start > now:
            changed.append(b.model_copy(update={"status": "planned"}))
        else:
            changed.append(b.model_copy(update={"status": "missed"}))
    return changed, deleted


def mark_missed(blocks: list[Block], now: datetime, grace_minutes: int = 15) -> list[Block]:
    """Blocks that were pinged and never started within the grace window, or planned blocks too
    late to ping (server was off, or the clock jumped), as copies with status 'missed'.
    Returns only the changed blocks."""
    grace = timedelta(minutes=grace_minutes)
    out = []
    for b in blocks:
        if (b.status == "pinged" and now > b.start + grace) or (
            b.status == "planned" and now >= b.start + MAX_LATE_PING
        ):
            out.append(b.model_copy(update={"status": "missed"}))
    return out
