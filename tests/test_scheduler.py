"""Planning blocks, firing pings and marking missed starts."""

from datetime import datetime, timedelta

from app.models import Assignment, Block, PrepCard
from app.scheduler import ceil_slot, due_ping, find_slot, mark_missed, plan_blocks
from tests.conftest import TZ, at

WINDOW = (15, 21)


def assignment(id, due, submitted=False):
    return Assignment(id=id, course_id=1, name=f"A{id}", due_at=due, submitted=submitted)


def card(id, effort):
    return PrepCard(assignment_id=id, kind="other", summary="", first_step="", steps=[], effort=effort)


def test_ceil_slot():
    assert ceil_slot(at(2, 15, 0)) == at(2, 15, 0)
    assert ceil_slot(at(2, 15, 1)) == at(2, 15, 15)
    assert ceil_slot(at(2, 15, 50)) == at(2, 16, 0)


def test_find_slot_respects_window_and_busy():
    busy = [(at(2, 15), at(2, 16))]
    assert find_slot(at(2, 9), timedelta(hours=1), busy, window=WINDOW) == at(2, 16)
    # too late in the day -> next day's window
    assert find_slot(at(2, 20, 30), timedelta(hours=1), [], window=WINDOW) == at(3, 15)
    # can't finish before the deadline
    assert find_slot(at(2, 20, 30), timedelta(hours=1), [], deadline=at(2, 23), window=WINDOW) is None


def test_plan_blocks_works_back_from_due_date():
    now = at(2, 10)  # Monday morning
    blocks = plan_blocks(
        [assignment(1, at(6, 23, 59)), assignment(2, at(3, 23, 59))],
        [card(1, "heavy"), card(2, "light")],
        now,
        window=WINDOW,
    )
    by_id = {b.assignment_id: b for b in blocks}
    # light, due Tuesday -> starts the day before (Monday) in the window, 30 min
    assert by_id[2].start == at(2, 15) and by_id[2].end - by_id[2].start == timedelta(minutes=30)
    # heavy, due Friday -> three days ahead (Tuesday), 90 min
    assert by_id[1].start == at(3, 15) and by_id[1].end - by_id[1].start == timedelta(minutes=90)
    assert not any(b.at_risk for b in blocks)


def test_window_holds_across_daylight_saving_change():
    """US clocks fall back on Sun Nov 1 2026. With Saturday's window full, the block must land at
    3 PM Sunday local time, not 3 PM in Saturday's UTC offset (which would be 2 PM)."""
    saturday = lambda hour: datetime(2026, 10, 31, hour, tzinfo=TZ)  # noqa: E731
    start = find_slot(saturday(9), timedelta(hours=1), [(saturday(15), saturday(21))], window=WINDOW)
    assert start == at(1, 15)
    assert start.utcoffset() == timedelta(hours=-8)


def test_plan_blocks_never_overlaps():
    now = at(2, 10)
    blocks = plan_blocks([assignment(i, at(3, 23, 59)) for i in range(1, 5)], [], now, window=WINDOW)
    spans = sorted((b.start, b.end) for b in blocks)
    assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:]))


def test_plan_blocks_skips_done_submitted_and_covered():
    now = at(2, 10)
    existing = [
        Block(id=1, assignment_id=1, start=at(2, 15), end=at(2, 16)),
        Block(id=2, assignment_id=3, start=at(1, 15), end=at(1, 16), status="done"),
    ]
    new = plan_blocks(
        [assignment(1, at(4, 23)), assignment(2, at(4, 23), submitted=True), assignment(3, at(4, 23)), assignment(4, at(1, 9))],
        [],
        now,
        existing,
        window=WINDOW,
    )
    assert new == []


def test_due_tonight_goes_outside_window_rather_than_nowhere():
    now = at(2, 21, 30)  # after the window closed
    [b] = plan_blocks([assignment(1, at(2, 23, 59))], [card(1, "light")], now, window=WINDOW)
    assert b.start == at(2, 21, 30) and not b.at_risk


def test_impossible_fit_is_flagged():
    now = at(2, 23, 30)
    [b] = plan_blocks([assignment(1, at(2, 23, 59))], [card(1, "heavy")], now, window=WINDOW)
    assert b.at_risk


def test_stale_missed_block_gets_replanned():
    now = at(3, 10)
    missed = Block(id=1, assignment_id=1, start=at(2, 15), end=at(2, 16), status="missed")
    assert len(plan_blocks([assignment(1, at(5, 23))], [], now, [missed], window=WINDOW)) == 1
    recent = missed.model_copy(update={"start": at(3, 8), "end": at(3, 9)})
    assert plan_blocks([assignment(1, at(5, 23))], [], now, [recent], window=WINDOW) == []


def test_due_ping_and_mark_missed():
    planned = Block(id=1, assignment_id=1, start=at(2, 16), end=at(2, 17))
    assert due_ping([planned], at(2, 15, 59)) is None
    assert due_ping([planned], at(2, 16)) == planned

    pinged = planned.model_copy(update={"status": "pinged"})
    assert mark_missed([pinged], at(2, 16, 10)) == []
    [missed] = mark_missed([pinged], at(2, 16, 16))
    assert missed.status == "missed"

    # a planned block the server never got to ping is missed, not pinged an hour late
    assert due_ping([planned], at(2, 17, 1)) is None
    assert mark_missed([planned], at(2, 17, 1))[0].status == "missed"
