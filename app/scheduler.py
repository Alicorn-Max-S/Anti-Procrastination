"""Turns assignments into start-time blocks and decides when a ping should fire.

Pure logic over models.py types; no network, no DB (callers load/save with db.py). Works backwards
from due_at: heavier assignments get longer blocks that start more days ahead, blocks never
overlap, and they sit inside a daily window (WORK_START_HOUR to WORK_END_HOUR, local time). Only
when nothing fits before the due date does a block go outside the window or get flagged at_risk.
A test gets several short practice sessions instead, one on each of the days before it, and a
quick task (a form, a signature) one QUICK_MINUTES block.
"""

from datetime import datetime, timedelta

from app.clock import local_tz
from app.config import settings
from app.models import Assignment, Block, PrepCard

SLOT = timedelta(minutes=15)
DURATION_MINUTES = {"light": 30, "medium": 60, "heavy": 90}
LEAD_DAYS = {"light": 1, "medium": 2, "heavy": 3}
PRACTICE_MINUTES = 20  # one practice session for a test
PRACTICE_SESSIONS = {"light": 2, "medium": 3, "heavy": 4}
PRACTICE_GAP = timedelta(hours=2)  # two practice sessions on one day sit at least this far apart
QUICK_MINUTES = 10  # a quick task's block
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


def is_test(a: Assignment, card: PrepCard | None) -> bool:
    """Whether the student sits this one (its blocks are practice sessions). The card's kind wins;
    before there's a card, the title guess and Canvas quizzes count."""
    if card is not None:
        return card.kind == "test"
    return a.kind == "test" or "online_quiz" in a.submission_types


def block_minutes(a: Assignment, card: PrepCard | None) -> int:
    """How long one block for this assignment runs: a quick task's, a test's practice session, or
    a work block by effort."""
    if card is not None and card.quick:
        return QUICK_MINUTES
    if is_test(a, card):
        return PRACTICE_MINUTES
    return DURATION_MINUTES[card.effort if card else "medium"]


def _practice(
    a: Assignment, effort: str, mine: list[Block], busy: Busy, earliest: datetime, window: Window
) -> list[Block]:
    """The practice sessions a test still needs: PRACTICE_SESSIONS[effort] minus the live ones in
    `mine`. A fresh plan puts one on each of the days before the test, the last on the day before
    (or today, if that's later); a missed one's replacement goes at the soonest time. Sessions of
    one test sit PRACTICE_GAP apart, so days short of sessions get two. A session that can't be
    spaced in the window before the test is left out, but a test always gets at least one.
    Adds the new ones to `busy`."""
    need = PRACTICE_SESSIONS[effort] - len(mine)
    length = timedelta(minutes=PRACTICE_MINUTES)
    tz = local_tz()
    today = earliest.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    last = max(today, (a.due_at.astimezone(tz) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
    days = [today] * need if mine else [max(today, last - timedelta(days=need - 1 - i)) for i in range(need)]
    spaced = [(b.start - PRACTICE_GAP, b.end + PRACTICE_GAP) for b in mine]
    out: list[Block] = []
    for day in days:
        start = find_slot(max(earliest, day), length, busy + spaced, a.due_at, window) or find_slot(
            earliest, length, busy + spaced, a.due_at, window
        )
        at_risk = False
        if start is None:
            if mine or out:
                continue
            start = find_slot(earliest, length, busy, a.due_at, None)
            at_risk = start is None
            start = start or find_slot(earliest, length, busy) or earliest
        block = Block(assignment_id=a.id, start=start, end=start + length, at_risk=at_risk)
        out.append(block)
        busy.append((block.start, block.end))
        spaced.append((block.start - PRACTICE_GAP, block.end + PRACTICE_GAP))
    return out


def plan_blocks(
    assignments: list[Assignment],
    cards: list[PrepCard],
    now: datetime,
    existing: list[Block] | None = None,
    window: Window | None = None,
) -> list[Block]:
    """Propose planned Blocks for every unfinished assignment due after `now` that isn't marked
    done: one work block, or for a test its practice sessions (see `_practice`). An assignment
    with live blocks gets nothing more, except a test short of sessions (a missed one's
    replacement). Returns only the new blocks (without ids)."""
    window = window or default_window()
    by_card = {c.assignment_id: c for c in cards}
    busy: Busy = []
    live: dict[int, list[Block]] = {}
    done: set[int] = set()
    for b in existing or []:
        if _is_live(b, now):
            busy.append((b.start, b.end))
            live.setdefault(b.assignment_id, []).append(b)
        if b.status == "done":
            done.add(b.assignment_id)

    todo = sorted(
        (a for a in assignments if not a.submitted and a.due_at and a.due_at > now and a.id not in done),
        key=lambda a: a.due_at,
    )
    earliest = ceil_slot(now)
    tz = local_tz()
    new: list[Block] = []
    for a in todo:
        card = by_card.get(a.id)
        e = card.effort if card else "medium"
        if is_test(a, card):
            new += _practice(a, e, live.get(a.id, []), busy, earliest, window)
            continue
        if a.id in live:
            continue
        length = timedelta(minutes=block_minutes(a, card))
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
