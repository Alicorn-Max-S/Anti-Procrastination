"""The check-in: which blocks are waiting on the student, what times to offer, and the answer
(start now / +15 min / pick a time / keep) with the week's reflow.

`waiting` decides what the check-in shows; `choices` the times "pick a time" offers, soonest
first. `apply_answer` takes the student's answer for one block and returns the updated full block
list plus the Ping recording the answer. Reflow: the answered block goes where the student said;
any planned block it now overlaps (or that slid into the past) moves to the next free slot, in the
daily window and before its due date if possible; everything else stays put. Blocks that end up
finishing after their assignment is due are flagged `at_risk` rather than squeezed somewhere
unworkable.
"""

from datetime import datetime, timedelta

from app.clock import as_local
from app.models import Assignment, Block, Ping
from app.scheduler import ACTIVE, Window, ceil_slot, default_window, find_slot, overlapping

SNOOZE = timedelta(minutes=15)
LEAD = timedelta(minutes=15)  # the soonest suggested start is at least this far out
TONIGHT_HOUR = 19


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


def choices(
    blocks: list[Block],
    block_id: int,
    now: datetime,
    due: datetime | None = None,
    window: Window | None = None,
) -> list[dict]:
    """Times to offer for "pick a time", as [{"key", "start"}], soonest first.

    "soonest" is the default: the first quarter hour at least LEAD out where the block fits in the
    work window, clear of the other live blocks, and ends by the due date (failing that, the first
    such slot outside the window, then after the due date). Then one-tap extras, each only if it
    ends by the due date: "in_30" and "in_60" (straight from now, the reflow makes room), "tonight"
    (first free slot from 7 PM today) and "tomorrow" (first free slot from tomorrow's work start).
    Duplicate times are dropped.
    """
    window = window or default_window()
    target = next(b for b in blocks if b.id == block_id)
    length = target.end - target.start
    busy = [(b.start, b.end) for b in blocks if b.id != block_id and b.status in ACTIVE]
    first = ceil_slot(now + LEAD)
    soonest = (
        find_slot(first, length, busy, due, window)
        or find_slot(first, length, busy, due)
        or find_slot(first, length, busy, window=window)
        or first
    )
    local = as_local(now)
    today_eve = local.replace(hour=TONIGHT_HOUR, minute=0, second=0, microsecond=0)
    tomorrow = (local + timedelta(days=1)).replace(hour=window[0], minute=0, second=0, microsecond=0)
    tonight = find_slot(max(first, today_eve), length, busy, due, window)
    next_day = find_slot(tomorrow, length, busy, due, window)
    extras = [
        ("in_30", ceil_slot(now + timedelta(minutes=30))),
        ("in_60", ceil_slot(now + timedelta(minutes=60))),
        ("tonight", tonight if tonight and as_local(tonight).date() == local.date() else None),
        ("tomorrow", next_day if next_day and as_local(next_day).date() == tomorrow.date() else None),
    ]
    out, seen = [{"key": "soonest", "start": soonest}], {soonest}
    for key, start in extras:
        if start is None or start in seen or (due and start + length > due):
            continue
        out.append({"key": key, "start": start})
        seen.add(start)
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
) -> tuple[list[Block], Ping]:
    """Apply 'start_now' | 'plus_15' | 'pick_time' | 'keep' to a block; return reflowed blocks + the Ping.

    'keep' leaves the block where it is (the student OKs the time a missed start was moved to).
    `ping` is the notification being answered (a new one is made if the student answers before
    any ping went out); `due` maps assignment id -> due date for the at-risk flag.
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
    return reflow(others, now, due=due, pinned=block_id, window=window), record


def reflow(
    blocks: list[Block],
    now: datetime,
    due: dict[int, datetime] | None = None,
    pinned: int | None = None,
    window: Window | None = None,
) -> list[Block]:
    """Resolve overlaps and anything now in the past after a change. Order by start.

    Pinged and started blocks and the `pinned` block (the one just answered) stay put; other
    planned blocks only move if they overlap one of those or start before `now`.
    """
    window = window or default_window()
    due = due or {}
    fixed, movable, rest = [], [], []
    for b in blocks:
        if b.id == pinned or b.status in ("pinged", "started"):
            fixed.append(b)
        elif b.status == "planned":
            movable.append(b)
        else:  # missed / done keep their history
            rest.append(b)

    # Pass 1: blocks that don't collide with anything stay exactly where they are.
    busy = [(b.start, b.end) for b in fixed]
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
            find_slot(from_, length, busy, d, window)
            or find_slot(from_, length, busy, d)
            or find_slot(from_, length, busy, window=window)
            or find_slot(from_, length, busy)
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
