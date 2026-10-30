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


# ---- "pick a time" suggestions -------------------------------------------------------------------


def one(start, end, id=1, status="missed"):
    return Block(id=id, assignment_id=id * 10, start=start, end=end, status=status)


def starts(blocks, block_id, now, due=None):
    from app.checkin import choices

    return {c["key"]: c["start"] for c in choices(blocks, block_id, now, due, window=WINDOW)}


def test_soonest_is_the_next_quarter_hour_at_least_15_minutes_out():
    # Max's case: a 4:30 PM start missed at 4:30. Not 7 PM: 4:45.
    miss = one(at(2, 16, 30), at(2, 18))
    assert starts([miss], 1, at(2, 16, 30), due=at(3, 23))["soonest"] == at(2, 16, 45)
    assert starts([miss], 1, at(2, 16, 31), due=at(3, 23))["soonest"] == at(2, 17)


def test_soonest_stays_in_the_work_window_and_clear_of_other_blocks():
    miss = one(at(2, 16), at(2, 17))
    other = one(at(2, 16, 45), at(2, 17, 45), id=2, status="planned")
    assert starts([miss, other], 1, at(2, 16, 30), due=at(3, 23))["soonest"] == at(2, 17, 45)
    assert starts([miss], 1, at(2, 10), due=at(3, 23))["soonest"] == at(2, 15)  # school hours: 3 PM
    assert starts([miss], 1, at(2, 20, 50), due=at(4, 23))["soonest"] == at(3, 15)  # after 9 PM: tomorrow


def test_soonest_leaves_the_window_rather_than_miss_the_due_date():
    miss = one(at(2, 8), at(2, 9))
    assert starts([miss], 1, at(2, 10), due=at(2, 13))["soonest"] == at(2, 10, 15)


def test_one_tap_choices_show_real_times_and_respect_the_due_date():
    miss = one(at(2, 16, 30), at(2, 17, 30))
    s = starts([miss], 1, at(2, 16, 30), due=at(4, 23))
    assert s == {
        "soonest": at(2, 16, 45),
        "in_30": at(2, 17),
        "in_60": at(2, 17, 30),
        "tonight": at(2, 19),
        "tomorrow": at(3, 15),
    }
    # due tonight at 8: "tonight" (7-8 PM) still fits, tomorrow doesn't
    assert "tomorrow" not in starts([miss], 1, at(2, 16, 30), due=at(2, 20))
    assert "tonight" not in starts([miss], 1, at(2, 16, 30), due=at(2, 19, 30))
