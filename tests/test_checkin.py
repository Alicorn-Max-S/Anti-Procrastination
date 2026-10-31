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


# ---- one-tap times: "In 15 min" and "Next work time" -------------------------------------------------


def one(start, end, id=1, status="missed"):
    return Block(id=id, assignment_id=id * 10, start=start, end=end, status=status)


def next_time(blocks, block_id, now, due=None):
    from app.checkin import next_work_time

    return next_work_time(blocks, block_id, now, due, window=WINDOW)


def offered(blocks, block_id, now, due=None):
    from app.checkin import choices

    return {c["key"]: c["start"] for c in choices(blocks, block_id, now, due, window=WINDOW)}


def test_next_work_time_is_the_next_quarter_hour_at_least_15_minutes_out():
    # Max's case: a 4:30 PM start missed at 4:30. Not 7 PM: 4:45.
    miss = one(at(2, 16, 30), at(2, 18))
    assert next_time([miss], 1, at(2, 16, 30), due=at(3, 23)) == at(2, 16, 45)
    assert next_time([miss], 1, at(2, 16, 31), due=at(3, 23)) == at(2, 17)


def test_next_work_time_stays_in_the_work_window_and_clear_of_other_blocks():
    miss = one(at(2, 16), at(2, 17))
    other = one(at(2, 16, 45), at(2, 17, 45), id=2, status="planned")
    assert next_time([miss, other], 1, at(2, 16, 30), due=at(3, 23)) == at(2, 17, 45)
    assert next_time([miss], 1, at(2, 10), due=at(3, 23)) == at(2, 15)  # school hours: 3 PM
    assert next_time([miss], 1, at(2, 20, 50), due=at(4, 23)) == at(3, 15)  # after 9 PM: tomorrow


def test_next_work_time_leaves_the_window_rather_than_miss_the_due_date():
    miss = one(at(2, 8), at(2, 9))
    assert next_time([miss], 1, at(2, 10), due=at(2, 13)) == at(2, 10, 15)


def test_in_15_only_inside_the_work_window():
    miss = one(at(2, 19), at(2, 20))
    # Max's case: 10:15 PM, after the window. No "In 15 min"; the next work time is tomorrow at 3.
    assert offered([miss], 1, at(2, 22, 15), due=at(4, 23)) == {"next": at(3, 15)}
    # 4:30 PM, nothing else planned: both would be 4:45, so only "In 15 min"
    assert offered([miss], 1, at(2, 16, 30), due=at(4, 23)) == {"in_15": at(2, 16, 45)}
    # 4:30 PM with 4:30-7 PM taken: "In 15 min" pushes that block; "Next work time" is the free 7 PM
    busy = one(at(2, 16, 30), at(2, 19), id=2, status="planned")
    assert offered([miss, busy], 1, at(2, 16, 30), due=at(4, 23)) == {"in_15": at(2, 16, 45), "next": at(2, 19)}
    # due at 5 PM: 4:45 + 1 h runs past it, so no "In 15 min"
    assert "in_15" not in offered([miss], 1, at(2, 16, 30), due=at(2, 17))


def test_next_work_time_is_where_a_miss_was_already_moved():
    from app.checkin import choices

    again = one(at(2, 22, 30), at(2, 23, 30), status="planned")  # a miss's new block, after hours
    later = one(at(2, 23, 30), at(3, 0), id=2, status="planned")
    # At 10:15:30 the rule alone says midnight (10:45 runs into 11:30); the button says 10:30, where it is.
    got = choices([again, later], 1, at(2, 22, 15) + timedelta(seconds=30), due=at(3, 8, 20), window=WINDOW, planned=again.start)
    assert got == [{"key": "next", "start": at(2, 22, 30)}]
    # once that time has passed, the rule decides again
    got = choices([again, later], 1, at(2, 22, 31), due=at(3, 8, 20), window=WINDOW, planned=again.start)
    assert got == [{"key": "next", "start": at(3, 0)}]
