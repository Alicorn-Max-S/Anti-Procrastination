"""Check-in answers and the week reflow."""

from datetime import timedelta

import pytest

from app.checkin import apply_answer, reflow
from app.models import Block, Ping
from tests.conftest import at

WINDOW = (15, 21)


def week():
    return [
        Block(id=1, assignment_id=10, start=at(2, 16), end=at(2, 17), status="missed"),
        Block(id=2, assignment_id=20, start=at(2, 19), end=at(2, 20)),
        Block(id=3, assignment_id=30, start=at(2, 20), end=at(2, 21)),
        Block(id=4, assignment_id=40, start=at(3, 15), end=at(3, 16)),
    ]


def test_start_now():
    blocks, ping = apply_answer(week(), 1, "start_now", at(2, 16, 20), window=WINDOW)
    b1 = next(b for b in blocks if b.id == 1)
    assert b1.status == "started" and b1.start == at(2, 16, 20) and b1.end == at(2, 17, 20)
    assert ping.answer == "start_now" and ping.started_at == at(2, 16, 20)


def test_plus_15_keeps_existing_ping_record():
    existing = Ping(id=7, block_id=1, sent_at=at(2, 16))
    blocks, ping = apply_answer(week(), 1, "plus_15", at(2, 16, 5), ping=existing, window=WINDOW)
    b1 = next(b for b in blocks if b.id == 1)
    assert b1.status == "planned" and b1.start == at(2, 16, 20)
    assert ping.id == 7 and ping.answer == "plus_15" and ping.sent_at == at(2, 16)


def test_pick_time_moves_only_the_block_in_the_way():
    """'After practice, around 7': the missed block takes 7 PM and the week makes room."""
    due = {10: at(4, 23), 20: at(4, 23), 30: at(2, 23, 59), 40: at(5, 23)}
    blocks, ping = apply_answer(week(), 1, "pick_time", at(2, 16, 30), picked_time=at(2, 19), due=due, window=WINDOW)
    by_id = {b.id: b for b in blocks}
    assert by_id[1].start == at(2, 19) and by_id[1].status == "planned"
    assert by_id[2].start == at(3, 16)  # displaced: next free window slot, after block 4
    assert by_id[3].start == at(2, 20)  # not in the way, so untouched
    assert by_id[4].start == at(3, 15)
    assert not any(b.at_risk for b in blocks)
    assert ping.picked_time == at(2, 19)
    starts = [b.start for b in blocks]
    assert starts == sorted(starts)


def test_displaced_block_due_tonight_stays_tonight_or_is_flagged():
    # due at 11:59 PM: leaves the window rather than miss the deadline
    due = {20: at(2, 23, 59)}
    blocks, _ = apply_answer(week(), 1, "pick_time", at(2, 16, 30), picked_time=at(2, 19), due=due, window=WINDOW)
    b2 = next(b for b in blocks if b.id == 2)
    assert b2.start == at(2, 21) and not b2.at_risk

    # due at 9:30 PM: nothing fits, so it's flagged instead
    due = {20: at(2, 21, 30)}
    blocks, _ = apply_answer(week(), 1, "pick_time", at(2, 16, 30), picked_time=at(2, 19), due=due, window=WINDOW)
    b2 = next(b for b in blocks if b.id == 2)
    assert b2.at_risk


def test_pick_time_needs_a_time():
    with pytest.raises(ValueError):
        apply_answer(week(), 1, "pick_time", at(2, 16))
    with pytest.raises(KeyError):
        apply_answer(week(), 99, "start_now", at(2, 16))


def test_reflow_moves_past_planned_blocks_but_not_history():
    blocks = [
        Block(id=1, assignment_id=1, start=at(2, 15), end=at(2, 16)),
        Block(id=2, assignment_id=2, start=at(2, 14), end=at(2, 15), status="done"),
    ]
    out = reflow(blocks, at(2, 15, 40), window=WINDOW)
    by_id = {b.id: b for b in out}
    assert by_id[1].start == at(2, 15, 45)
    assert by_id[2].start == at(2, 14)
    assert by_id[1].end - by_id[1].start == timedelta(hours=1)


def test_keep_leaves_the_block_and_records_it():
    b = Block(id=5, assignment_id=50, start=at(2, 17), end=at(2, 18))
    blocks, ping = apply_answer(week() + [b], 5, "keep", at(2, 16, 30), window=WINDOW)
    assert next(x for x in blocks if x.id == 5).start == at(2, 17)
    assert ping.answer == "keep" and ping.picked_time is None and ping.started_at is None
