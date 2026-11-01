"""The check-in: which blocks are waiting on the student, what times to offer, and the answer
(start now / +15 min / pick a time / keep) with the week's reflow.

`waiting` decides what the check-in shows; `choices` the one-tap times it offers ("In 15 min",
"Next work time"). `apply_answer` takes the student's answer for one block and returns the updated full block
list plus the Ping recording the answer. Reflow: the answered block goes where the student said;
any planned block it now overlaps (or that slid into the past) moves to the next free slot, in the
daily window and before its due date if possible; everything else stays put. Blocks that end up
finishing after their assignment is due are flagged `at_risk` rather than squeezed somewhere
unworkable.
"""

from datetime import datetime, timedelta

from app.clock import as_local
from app.models import Assignment, Availability, Block, Ping
from app.scheduler import (
    ACTIVE,
    Window,
    ceil_slot,
    default_window,
    find_slot,
    off_times,
    open_times,
    overlapping,
)

SNOOZE = timedelta(minutes=15)
LEAD = timedelta(minutes=15)  # the next work time is at least this far out
NEAR = timedelta(minutes=30)  # a next work time this close to "In 15 min" is the same choice twice


def closed(assignments: dict[int, Assignment], blocks: list[Block], now: datetime) -> set[int]:
    """Assignments a missed start no longer asks about: done, submitted, or past due."""
    done = {b.assignment_id for b in blocks if b.status == "done"}
    return done | {a.id for a in assignments.values() if a.submitted or (a.due_at and a.due_at < now)}


def waiting(blocks: list[Block], pings: list[Ping], closed_ids: set[int]) -> list[Block]:
    """Blocks waiting on the student: pinged ones, plus each open assignment's latest unanswered miss.

    A miss is answered once a ping sent at or after its start has an answer, and it drops out when
    a later block of the same assignment is pinged or started. No age limit: a miss found after a
    big clock jump still asks. `closed_ids` (see `closed`) are skipped: their card says past due instead.
    """
    start = {b.id: b.start for b in blocks}
    answered = {
        p.block_id for p in pings if p.answer != "none" and p.block_id in start and p.sent_at >= start[p.block_id]
    }
    latest_miss: dict[int, Block] = {}
    for b in sorted(blocks, key=lambda b: b.start):
        if b.status == "missed":
            latest_miss[b.assignment_id] = b  # the last one wins

    def superseded(m: Block) -> bool:
        return any(
            b.assignment_id == m.assignment_id and b.status in ("pinged", "started") and b.start > m.start
            for b in blocks
        )

    misses = [m for a, m in latest_miss.items() if a not in closed_ids and m.id not in answered and not superseded(m)]
    pinged = [b for b in blocks if b.status == "pinged"]
    return sorted(pinged + misses, key=lambda b: b.start)


def replacement(blocks: list[Block], missed: Block) -> Block | None:
    """The live block that took over from a missed one: same assignment, later, planned or pinged."""
    later = [
        b
        for b in blocks
        if b.assignment_id == missed.assignment_id and b.status in ("planned", "pinged") and b.start > missed.start
    ]
    return min(later, key=lambda b: b.start) if later else None


def next_work_time(
    blocks: list[Block],
    block_id: int,
    now: datetime,
    due: datetime | None = None,
    window: Window | None = None,
    avail: list[Availability] | None = None,
) -> datetime:
    """The first quarter hour at least LEAD out where the block fits in the work window (or time
    the student said they can work), clear of the other live blocks and of time they said they
    can't work, and ends by the due date; failing that, the first such slot outside the window,
    then after the due date."""
    window = window or default_window()
    target = next(b for b in blocks if b.id == block_id)
    length = target.end - target.start
    busy = [(b.start, b.end) for b in blocks if b.id != block_id and b.status in ACTIVE] + off_times(avail)
    open_ = open_times(avail)
    first = ceil_slot(now + LEAD)
    return (
        find_slot(first, length, busy, due, window, open_=open_)
        or find_slot(first, length, busy, due)
        or find_slot(first, length, busy, window=window, open_=open_)
        or find_slot(first, length, busy)
        or first
    )


def choices(
    blocks: list[Block],
    block_id: int,
    now: datetime,
    due: datetime | None = None,
    window: Window | None = None,
    planned: datetime | None = None,
    avail: list[Availability] | None = None,
) -> list[dict]:
    """The one-tap times a check-in offers besides Start now, as [{"key", "start"}]: "in_15" (the
    "+15 min" answer) only when that falls inside the work window (or time the student said they
    can work), clear of time they said they can't, and the block still ends by the due date, then
    "next" unless it's within NEAR of "in_15", so the two never offer the same thing. "next" is
    `planned` when given and still ahead (where a missed start was already moved, so the button
    agrees with what happens if the student does nothing), else `next_work_time`."""
    window = window or default_window()
    target = next(b for b in blocks if b.id == block_id)
    length = target.end - target.start
    snooze = now + SNOOZE
    soonest = planned if planned and planned > now else next_work_time(blocks, block_id, now, due, window, avail)
    out = []
    working = window[0] <= as_local(snooze).hour < window[1] or any(s <= snooze < e for s, e in open_times(avail))
    if working and not overlapping(snooze, snooze + length, off_times(avail)) and not (due and snooze + length > due):
        out.append({"key": "in_15", "start": snooze})
    if not out or abs(soonest - snooze) >= NEAR:
        out.append({"key": "next", "start": soonest})
    return out


def apply_answer(
    blocks: list[Block],
    block_id: int,
    answer: str,
    now: datetime,
    picked_time: datetime | None = None,
    ping: Ping | None = None,
    due: dict[int, datetime] | None = None,
    window: Window | None = None,
    avail: list[Availability] | None = None,
) -> tuple[list[Block], Ping]:
    """Apply 'start_now' | 'plus_15' | 'pick_time' | 'keep' to a block; return reflowed blocks + the Ping.

    'keep' leaves the block where it is (the student OKs the time a missed start was moved to).
    `ping` is the notification being answered (a new one is made if the student answers before
    any ping went out); `due` maps assignment id -> due date for the at-risk flag; `avail` keeps
    the reflow out of time the student said they can't work.
    """
    target = next((b for b in blocks if b.id == block_id), None)
    if target is None:
        raise KeyError(f"no block {block_id}")
    length = target.end - target.start
    if answer == "start_now":
        start, status = now, "started"
    elif answer == "plus_15":
        start, status = now + SNOOZE, "planned"
    elif answer == "pick_time":
        if picked_time is None:
            raise ValueError("pick_time needs picked_time")
        start, status = as_local(picked_time), "planned"
    elif answer == "keep":
        start, status = target.start, target.status
    else:
        raise ValueError(f"unknown answer {answer!r}")

    moved = target.model_copy(update={"start": start, "end": start + length, "status": status})
    record = (ping or Ping(block_id=block_id, sent_at=now)).model_copy(
        update={
            "answer": answer,
            "picked_time": start if answer == "pick_time" else None,
            "started_at": now if answer == "start_now" else None,
        }
    )
    others = [moved if b.id == block_id else b for b in blocks]
    return reflow(others, now, due=due, pinned=block_id, window=window, avail=avail), record


def reflow(
    blocks: list[Block],
    now: datetime,
    due: dict[int, datetime] | None = None,
    pinned: int | None = None,
    window: Window | None = None,
    avail: list[Availability] | None = None,
) -> list[Block]:
    """Resolve overlaps and anything now in the past after a change. Order by start.

    Pinged and started blocks and the `pinned` block (the one just answered) stay put; other
    planned blocks only move if they overlap one of those, overlap time the student said they
    can't work (`avail`), or start before `now`.
    """
    window = window or default_window()
    due = due or {}
    open_ = open_times(avail)
    fixed, movable, rest = [], [], []
    for b in blocks:
        if b.id == pinned or b.status in ("pinged", "started"):
            fixed.append(b)
        elif b.status == "planned":
            movable.append(b)
        else:  # missed / done keep their history
            rest.append(b)

    # Pass 1: blocks that don't collide with anything stay exactly where they are.
    busy = [(b.start, b.end) for b in fixed] + off_times(avail)
    placed, displaced = [], []
    for b in sorted(movable, key=lambda b: b.start):
        if b.start >= now and not overlapping(b.start, b.end, busy):
            placed.append(b)
            busy.append((b.start, b.end))
        else:
            displaced.append(b)
    # Pass 2: displaced blocks take the next free slot, preferring the window and their due date.
    for b in displaced:
        length = b.end - b.start
        from_ = max(b.start, now)
        d = due.get(b.assignment_id)
        start = (
            find_slot(from_, length, busy, d, window, open_=open_)
            or find_slot(from_, length, busy, d)
            or find_slot(from_, length, busy, window=window, open_=open_)
            or find_slot(from_, length, busy)
            or from_  # weeks of "can't work": nothing fits, so it stays flagged where it is
        )
        moved = b.model_copy(update={"start": start, "end": start + length})
        placed.append(moved)
        busy.append((moved.start, moved.end))

    out = []
    for b in fixed + placed + rest:
        if b.status in ("planned", "pinged", "started"):
            d = due.get(b.assignment_id)
            b = b.model_copy(update={"at_risk": bool(d and b.end > d)})
        out.append(b)
    return sorted(out, key=lambda b: b.start)
