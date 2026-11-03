"""Earliest due first: the planned week never puts work due later ahead of work due sooner, unless
the student put it there."""

from datetime import timedelta

from app import db, loop
from app.models import Assignment, Block, PrepCard
from app.scheduler import due_order
from tests.conftest import at

WINDOW = (15, 21)


def block(id, assignment_id, start, minutes, **kw):
    return Block(id=id, assignment_id=assignment_id, start=start, end=start + timedelta(minutes=minutes), **kw)


def starts(blocks):
    return {b.id: b.start for b in blocks}


def test_work_due_sooner_takes_the_earlier_slot_and_the_rest_follows_it():
    # the essay (due Friday) was planned first; the worksheet (due Tuesday) came later and landed after it
    essay, worksheet = block(1, 10, at(2, 15), 90), block(2, 20, at(2, 16, 30), 60)
    due = {10: at(6, 23, 59), 20: at(3, 23, 59)}
    moved = due_order([essay, worksheet], at(2, 10), due, WINDOW)
    assert starts(moved) == {2: at(2, 15), 1: at(2, 16)}


def test_a_later_day_comes_forward_and_heavy_work_keeps_its_day_when_it_fits():
    heavy = block(1, 10, at(2, 15), 90)  # due Thursday, planned Monday for its lead time
    light = block(2, 20, at(3, 15), 30)  # due Wednesday, planned Tuesday
    moved = due_order([heavy, light], at(2, 10), {10: at(5, 23, 59), 20: at(4, 23, 59)}, WINDOW)
    assert starts(moved) == {2: at(2, 15), 1: at(2, 15, 30)}


def test_pinned_session_and_started_blocks_stay():
    due = {10: at(6, 23, 59), 20: at(3, 23, 59), 30: at(3, 23, 59)}
    picked = block(1, 10, at(2, 15), 60, pinned=True)  # the student picked this time
    session = block(2, 10, at(2, 16), 30, session_id=5)
    late = block(3, 20, at(2, 17), 60)
    assert due_order([picked, session, late], at(2, 10), due, WINDOW) == []
    started = block(4, 10, at(2, 15), 60, status="started")
    assert due_order([started, block(5, 30, at(2, 16), 60)], at(2, 15, 5), due, WINDOW) == []


def test_nothing_moves_out_of_the_window_or_past_its_due_date():
    # the later-due block can't go after the other: it would end after it's due
    a, b = block(1, 10, at(2, 15), 60), block(2, 20, at(2, 16), 30)
    assert due_order([a, b], at(2, 10), {10: at(2, 16, 10), 20: at(2, 16, 5)}, WINDOW) == []
    # the window closes at 9 PM and 7-8 PM is taken, so there's no room to trade
    a, b = block(1, 10, at(2, 20), 30), block(2, 20, at(2, 20, 30), 60)
    taken = block(3, 30, at(2, 19), 60, pinned=True)
    assert due_order([a, b, taken], at(2, 10), {10: at(6, 23), 20: at(3, 23), 30: at(6, 23)}, WINDOW) == []


def test_a_practice_session_only_trades_places_within_its_day():
    quiz = block(1, 10, at(2, 15), 20)  # practice for Thursday's quiz, Monday
    lab = block(2, 20, at(3, 15), 90)  # due Wednesday, planned Tuesday
    due = {10: at(5, 9), 20: at(4, 23, 59)}
    # the lab comes forward to Monday and the practice stays on Monday, after it
    assert starts(due_order([quiz, lab], at(2, 10), due, WINDOW, tests={10})) == {2: at(2, 15), 1: at(2, 16, 30)}
    # with Monday full after 4:30, the practice would have to leave its day, so nothing trades
    full = block(3, 30, at(2, 16, 30), 270, pinned=True)
    assert due_order([quiz, lab, full], at(2, 10), {**due, 30: at(6, 23)}, WINDOW, tests={10}) == []


def test_replan_puts_new_work_due_sooner_ahead_of_what_was_planned(conn):
    essay = Assignment(id=1, course_id=1, name="Essay", due_at=at(6, 23, 59))
    db.save_assignment(conn, essay)
    db.save_card(conn, PrepCard(assignment_id=1, kind="essay", summary="", first_step="", steps=[], effort="heavy"))
    [planned] = loop.replan(conn, at(3, 10))
    assert planned.start == at(3, 15)  # three days ahead of Friday
    db.save_assignment(conn, Assignment(id=2, course_id=1, name="Worksheet", due_at=at(4, 23, 59)))
    db.save_card(conn, PrepCard(assignment_id=2, kind="problem_set", summary="", first_step="", steps=[], effort="medium"))
    [new] = loop.replan(conn, at(3, 10))
    assert new.start == at(3, 15)  # it went where the essay was, and the replan says so
    by_id = {b.assignment_id: b for b in db.list_blocks(conn)}
    assert by_id[2].start == at(3, 15) and by_id[1].start == at(3, 16)


def test_list_and_blocks_sort_on_real_times_not_text(conn):
    from datetime import timezone

    # stored in UTC, 7:59 AM in Los Angeles is "15:59+00:00", which sorts after "08:00-08:00" as text
    db.save_assignment(conn, Assignment(id=1, course_id=1, name="Later", due_at=at(4, 8)))
    db.save_assignment(conn, Assignment(id=2, course_id=1, name="Sooner", due_at=at(4, 7, 59).astimezone(timezone.utc)))
    assert [a.name for a in db.list_assignments(conn)] == ["Sooner", "Later"]
