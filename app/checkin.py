"""Handles the check-in answer (start now / +15 min / pick a time) and reflows the week.

Takes the student's answer for one block and returns the updated full block list plus the Ping
recording the answer. Reflow: the answered block goes where the student said; any planned block
it now overlaps (or that slid into the past) moves to the next free slot, in the daily window and
before its due date if possible; everything else stays put. Blocks that end up finishing after
their assignment is due are flagged `at_risk` rather than squeezed somewhere unworkable.
"""

from datetime import datetime, timedelta

from app.clock import as_local
from app.models import Block, Ping
from app.scheduler import Window, default_window, find_slot, overlapping

SNOOZE = timedelta(minutes=15)


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
    """Apply 'start_now' | 'plus_15' | 'pick_time' to a block; return reflowed blocks + the Ping.

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
