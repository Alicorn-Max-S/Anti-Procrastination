"""Telling the planner when you can and can't work: the spans bend every search, and the week moves
at once. The model is stubbed. Demo week (seeded Monday Nov 2, 10 AM): block 1 Problem Set 6
Mon 3-4 PM (due Tue night), 2 Lab 4 Mon 4-5:30 PM (due Wed night), 3 quiz practice Mon 5:30-5:50 PM,
4 and 5 more practice Tue and Wed 3 PM, 6 and 7 Thu 3 PM."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import availability, db, demo, loop, meta_client
from app.availability import fit
from app.checkin import choices, next_work_time, reflow
from app.config import settings
from app.main import app
from app.meta_client import MetaError
from app.models import Assignment, Availability, Block, PrepCard
from app.scheduler import find_slot, nearest_slot, plan_blocks
from tests.conftest import at

WINDOW = (15, 21)
HOUR = timedelta(hours=1)


def span(kind, start, end, note="", id=None):
    return Availability(id=id, kind=kind, start=start, end=end, note=note)


def planned(id, assignment_id, start, end, **kw):
    return Block(id=id, assignment_id=assignment_id, start=start, end=end, **kw)


# ---- the scheduler's searches --------------------------------------------------------------------


def test_find_slot_counts_open_time_beside_the_window():
    morning = [(at(2, 10), at(2, 12))]
    assert find_slot(at(2, 9), HOUR, [], window=WINDOW, open_=morning) == at(2, 10)
    assert find_slot(at(2, 9), HOUR, [], window=WINDOW) == at(2, 15)  # without it, the window alone
    # open time too short for the block is no use
    assert find_slot(at(2, 9), HOUR, [], window=WINDOW, open_=[(at(2, 10), at(2, 10, 30))]) == at(2, 15)
    # open time lets a block run past the window's close
    assert find_slot(at(2, 20, 30), HOUR, [], window=WINDOW, open_=[(at(2, 20), at(2, 23))]) == at(2, 20, 30)
    # an open span that starts between quarter hours is found from the next quarter hour
    assert find_slot(at(2, 9), HOUR, [], window=WINDOW, open_=[(at(2, 13, 10), at(2, 14, 40))]) == at(2, 13, 15)
    # busy time inside open time still counts
    assert find_slot(at(2, 9), HOUR, [(at(2, 10), at(2, 11))], window=WINDOW, open_=[(at(2, 10), at(2, 13))]) == at(2, 11)


def test_nearest_slot_takes_the_closer_side_and_the_earlier_on_a_tie():
    busy = [(at(2, 15), at(2, 18))]
    near = at(2, 16)
    assert nearest_slot(near, HOUR, busy, at(2, 9), window=WINDOW) == at(2, 18)  # nothing earlier in the window
    # open time until 3 PM: 2 PM is as far before as 6 PM is after, so the earlier one wins
    assert nearest_slot(near, HOUR, busy, at(2, 9), window=WINDOW, open_=[(at(2, 10), at(2, 15))]) == at(2, 14)
    # open time that ends at 2:30 only reaches 1:30, which is farther than 6 PM
    assert nearest_slot(near, HOUR, busy, at(2, 9), window=WINDOW, open_=[(at(2, 10), at(2, 14, 30))]) == at(2, 18)
    # nothing before `earliest`, and a deadline can rule out the later side
    assert nearest_slot(near, HOUR, busy, at(2, 14, 30), window=WINDOW, open_=[(at(2, 10), at(2, 15))]) == at(2, 18)
    assert nearest_slot(near, HOUR, busy, at(2, 9), at(2, 18, 30), WINDOW, [(at(2, 10), at(2, 15))]) == at(2, 14)


def test_plan_blocks_keeps_out_of_time_off_and_uses_open_time():
    a = Assignment(id=1, course_id=1, name="A1", due_at=at(3, 23, 59))
    card = PrepCard(assignment_id=1, kind="other", summary="", first_step="", steps=[], effort="light")  # 30 min, a day ahead
    [free] = plan_blocks([a], [card], at(2, 10), window=WINDOW)
    assert free.start == at(2, 15)
    [moved] = plan_blocks([a], [card], at(2, 10), window=WINDOW, avail=[span("off", at(2, 14), at(2, 18))])
    assert moved.start == at(2, 18)
    [early] = plan_blocks([a], [card], at(2, 10), window=WINDOW, avail=[span("open", at(2, 10), at(2, 12))])
    assert early.start == at(2, 10)


def test_practice_sessions_keep_out_of_time_off():
    quiz = Assignment(id=1, course_id=1, name="Quiz", due_at=at(6, 9))
    card = PrepCard(assignment_id=1, kind="test", summary="", first_step="", steps=[], effort="medium")
    off = [span("off", at(3, 0), at(4, 0))]  # all of Tuesday
    starts = [b.start for b in plan_blocks([quiz], [card], at(2, 10), window=WINDOW, avail=off)]
    assert at(3, 15) not in starts and len(starts) == 3
    assert all(not (at(3, 0) <= s < at(4, 0)) for s in starts)


# ---- fit: moving the week around what was said ---------------------------------------------------


def test_fit_moves_a_block_out_of_time_off_to_the_nearest_free_slot():
    blocks = [planned(1, 10, at(2, 15), at(2, 16)), planned(2, 20, at(3, 15), at(3, 16))]
    [moved] = fit(blocks, at(2, 10), [span("off", at(2, 14), at(2, 18))], window=WINDOW)
    assert (moved.id, moved.start, moved.end) == (1, at(2, 18), at(2, 19))


def test_fit_prefers_an_earlier_slot_when_it_is_closer():
    # Tuesday afternoon and evening are off and Wednesday is full: Monday 8 PM is nearer than Thursday
    blocks = [planned(1, 10, at(3, 15), at(3, 16)), planned(2, 20, at(4, 15), at(4, 21))]
    [moved] = fit(blocks, at(2, 10), [span("off", at(3, 12), at(3, 22))], window=WINDOW)
    assert (moved.id, moved.start) == (1, at(2, 20))


def test_fit_uses_open_time_earlier_the_same_day():
    # "busy this evening, but I can work this morning": the 3 PM block goes to the morning, not tomorrow
    blocks = [planned(1, 10, at(2, 15), at(2, 16))]
    avail = [span("off", at(2, 14), at(2, 22)), span("open", at(2, 10), at(2, 14))]
    [moved] = fit(blocks, at(2, 9), avail, window=WINDOW)
    assert moved.start == at(2, 13)  # the last hour before the time off, the closest to 3 PM


def test_fit_leaves_blocks_the_student_is_already_on():
    blocks = [
        planned(1, 10, at(2, 15), at(2, 16), status="pinged"),
        planned(2, 20, at(2, 16), at(2, 17), status="started"),
        planned(3, 30, at(2, 17), at(2, 18)),
    ]
    [moved] = fit(blocks, at(2, 15, 10), [span("off", at(2, 14), at(2, 17, 30))], window=WINDOW)
    assert (moved.id, moved.start) == (3, at(2, 17, 30))  # the first free time, clear of both blocks


def test_fit_leaves_history_and_a_block_about_to_be_pinged():
    blocks = [
        planned(1, 10, at(2, 15), at(2, 16)),  # start has passed: the next tick pings it
        planned(2, 20, at(2, 16), at(2, 17), status="missed"),
        planned(3, 30, at(2, 17), at(2, 18), status="done"),
    ]
    assert fit(blocks, at(2, 15, 5), [span("off", at(2, 14), at(2, 19))], window=WINDOW) == []
    assert fit(blocks, at(2, 15, 5), [], window=WINDOW) == []


def test_fit_keeps_a_block_due_tonight_before_its_deadline():
    b = planned(1, 10, at(2, 15), at(2, 16))
    [moved] = fit([b], at(2, 10), [span("off", at(2, 14), at(2, 21))], {10: at(2, 23, 59)}, window=WINDOW)
    assert moved.start == at(2, 21) and not moved.at_risk  # past the window's close, but in time
    [late] = fit([b], at(2, 10), [span("off", at(2, 14), at(2, 23, 30))], {10: at(2, 23, 59)}, window=WINDOW)
    assert late.at_risk  # nothing fits before it's due: flagged, not hidden


def test_fit_gives_a_block_running_out_of_time_another_try_in_open_time():
    late = planned(1, 10, at(2, 15), at(2, 16, 30), at_risk=True)  # ends after it's due at noon
    due = {10: at(2, 12)}
    [saved] = fit([late], at(2, 9), [span("open", at(2, 9, 30), at(2, 11, 30))], due, window=WINDOW)
    assert (saved.start, saved.end, saved.at_risk) == (at(2, 9, 30), at(2, 11), False)
    # still at risk (and untouched) when the new time isn't enough
    assert fit([late], at(2, 11), [span("open", at(2, 11), at(2, 11, 45))], due, window=WINDOW) == []


def test_fit_moves_blocks_out_of_open_time_that_was_taken_back():
    gone = span("open", at(2, 10), at(2, 12))
    in_the_morning = planned(1, 10, at(2, 10), at(2, 11))
    [moved] = fit([in_the_morning], at(2, 9), [], vacated=[gone], window=WINDOW)
    assert moved.start == at(2, 15)
    inside_the_window = planned(2, 20, at(2, 16), at(2, 17))
    assert fit([inside_the_window], at(2, 9), [], vacated=[span("open", at(2, 14), at(2, 18))], window=WINDOW) == []


def test_fit_flags_a_block_when_nothing_fits_for_weeks():
    b = planned(1, 10, at(2, 15), at(2, 16))
    [stuck] = fit([b], at(2, 10), [span("off", at(2, 0), at(2, 0) + timedelta(days=40))], window=WINDOW)
    assert (stuck.start, stuck.at_risk) == (b.start, True)


def test_fit_with_nothing_said_moves_nothing():
    assert fit([planned(1, 10, at(2, 15), at(2, 16))], at(2, 10), [], window=WINDOW) == []


# ---- the check-in keeps to the same spans --------------------------------------------------------


def pinged():
    return [Block(id=1, assignment_id=10, start=at(2, 16), end=at(2, 17), status="pinged")]


def test_next_work_time_and_the_one_tap_choices_skip_time_off():
    off = [span("off", at(2, 16), at(2, 19))]
    assert next_work_time(pinged(), 1, at(2, 16, 5), window=WINDOW, avail=off) == at(2, 19)
    got = choices(pinged(), 1, at(2, 16, 5), window=WINDOW, avail=off)
    assert [(c["key"], c["start"]) for c in got] == [("next", at(2, 19))]  # "in 15 min" would be time off


def test_open_time_makes_in_15_minutes_a_choice_after_hours():
    late = [span("open", at(2, 21), at(2, 23))]
    got = choices(pinged(), 1, at(2, 21, 50), window=WINDOW, avail=late)
    assert [c["key"] for c in got][0] == "in_15" and got[0]["start"] == at(2, 22, 5)
    assert "in_15" not in [c["key"] for c in choices(pinged(), 1, at(2, 21, 50), window=WINDOW)]


def test_a_reflow_keeps_displaced_blocks_out_of_time_off():
    blocks = [
        Block(id=1, assignment_id=10, start=at(2, 15), end=at(2, 16), status="pinged"),
        Block(id=2, assignment_id=20, start=at(2, 15, 30), end=at(2, 16, 30)),  # in the pinged block's way
    ]
    out = reflow(blocks, at(2, 15, 10), window=WINDOW, avail=[span("off", at(2, 16), at(2, 18))])
    assert next(b for b in out if b.id == 2).start == at(2, 18)


# ---- new blocks and storage ----------------------------------------------------------------------


def test_availability_round_trips_and_ended_spans_drop_out(conn):
    keep = db.save_availability(conn, span("off", at(2, 16), at(2, 18), "soccer"))
    db.save_availability(conn, span("open", at(1, 9), at(1, 10)))
    [got] = db.list_availability(conn, since=at(2, 9))
    assert got == span("off", at(2, 16), at(2, 18), "soccer", id=keep)
    assert len(db.list_availability(conn)) == 2
    db.delete_availability(conn, keep)
    assert db.get_availability(conn, keep) is None


def test_replan_keeps_new_blocks_out_of_time_off(conn):
    db.save_assignment(conn, Assignment(id=1, course_id=1, name="Essay", due_at=at(4, 23, 59)))
    db.save_availability(conn, span("off", at(2, 0), at(3, 21)))
    [b] = loop.replan(conn, at(2, 10))
    assert b.start == at(4, 15)  # Monday and Tuesday evening are off, so Wednesday: still before it's due


# ---- reading what the student wrote --------------------------------------------------------------


def reading(*spans_, unclear=""):
    return availability._Reading(spans=list(spans_), unclear=unclear)


def said(kind, date, start, end, note=""):
    return availability._Span(kind=kind, date=date, start_time=start, end_time=end, note=note)


@pytest.fixture
def model(monkeypatch):
    """Replace the model with a queue of canned replies (or exceptions)."""
    object.__setattr__(settings, "meta_api_key", "key")
    replies, prompts = [], []

    def fake(prompt, schema, **kw):
        prompts.append((prompt, schema, kw))
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(meta_client, "generate_structured", fake)
    return replies, prompts


def test_parse_turns_the_models_reading_into_spans(model):
    replies, prompts = model
    replies.append(reading(said("off", "2026-11-03", "17:00", "24:00", " soccer "), said("open", "2026-11-07", "09:00", "12:00")))
    spans, notes = availability.parse("can't work Tuesday after 5 (soccer), free Saturday morning", at(2, 10))
    assert [(a.kind, a.start, a.end, a.note) for a in spans] == [
        ("off", at(3, 17), at(4, 0), "soccer"),
        ("open", at(7, 9), at(7, 12), ""),
    ]
    assert notes == [] and all(a.id is None for a in spans)  # nothing is saved by reading
    prompt, schema, kw = prompts[0]
    assert schema is availability._Reading and kw["instructions"] == availability.INSTRUCTIONS
    assert "Mon 2026-11-02 (today)" in prompt and "Tue 2026-11-03" in prompt and "Sun 2026-11-15" in prompt
    assert "10:00" in prompt and "15:00 to 21:00" in prompt and "can't work Tuesday after 5" in prompt


def test_parse_drops_what_it_cannot_trust_and_says_so(model):
    replies, _ = model
    replies.append(
        reading(
            said("off", "2026-11-01", "09:00", "10:00"),  # already over
            said("off", "2026-11-03", "18:00", "17:00"),  # backwards
            said("off", "next tuesday", "17:00", "18:00"),  # not a date
            said("off", "2026-11-03", "9am", "10am"),  # not a time
            said("off", "2026-11-03", "25:00", "26:00"),  # not a real time
            said("off", "2026-11-03", "08:00", "09:00"),  # fine
            unclear="Which Friday?",
        )
    )
    spans, notes = availability.parse("whatever", at(2, 10))
    assert [(a.start, a.end) for a in spans] == [(at(3, 8), at(3, 9))]
    assert notes[0] == "Which Friday?" and len(notes) == 6


def test_parse_caps_how_many_spans_one_message_adds(model):
    replies, _ = model
    replies.append(reading(*[said("off", "2026-11-03", "08:00", "09:00") for _ in range(availability.MAX_SPANS + 5)]))
    spans, _ = availability.parse("every day", at(2, 10))
    assert len(spans) == availability.MAX_SPANS


# ---- through the HTTP API ------------------------------------------------------------------------


def set_clock(client, when):
    r = client.post("/api/clock", json={"action": "set", "when": when.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def client(conn):
    demo.seed(conn, now=at(2, 10))
    with TestClient(app) as c:
        set_clock(c, at(2, 10))
        yield c


def say(client, start, end, note="", kind="off"):
    entry = {"kind": kind, "start": start.isoformat(), "end": end.isoformat(), "note": note}
    return client.post("/api/availability", json={"entries": [entry]})


def starts(client):
    return {b["id"]: datetime.fromisoformat(b["start"]) for b in client.get("/api/state").json()["blocks"]}


def test_saying_you_cant_work_moves_the_week_at_once(client):
    r = say(client, at(2, 14), at(2, 18), "soccer")
    assert r.status_code == 200
    body = r.json()
    assert [(m["id"], datetime.fromisoformat(m["start"])) for m in body["moved"]] == [
        (1, at(2, 18)),
        (2, at(2, 19)),
        (3, at(2, 20, 30)),
    ]
    assert datetime.fromisoformat(body["moved"][0]["was"]["start"]) == at(2, 15)
    assert body["kept"] == [] and body["entries"][0]["note"] == "soccer" and body["entries"][0]["id"]
    assert starts(client)[1] == at(2, 18) and starts(client)[4] == at(3, 15)  # the other days are untouched
    assert [a["note"] for a in client.get("/api/state").json()["availability"]] == ["soccer"]


def test_a_block_you_are_already_on_stays_and_is_reported(client):
    set_clock(client, at(2, 15, 5))  # block 1 is pinged
    body = say(client, at(2, 15, 30), at(2, 17)).json()
    assert [k["id"] for k in body["kept"]] == [1]
    assert [(m["id"], datetime.fromisoformat(m["start"])) for m in body["moved"]] == [(2, at(2, 18))]
    assert starts(client)[1] == at(2, 15)


def test_the_check_in_does_not_offer_time_you_said_is_off(client):
    set_clock(client, at(2, 15, 20))  # block 1 is pinged
    say(client, at(2, 15, 30), at(2, 20))
    [pending] = client.get("/api/state").json()["checkin"]
    assert [c["key"] for c in pending["choices"]] == ["next"]  # 3:35 PM would run into the time off
    soonest = datetime.fromisoformat(pending["choices"][0]["start"])
    assert not (at(2, 15, 30) <= soonest < at(2, 20))


def test_taking_a_span_back(client):
    saved = say(client, at(2, 14), at(2, 18)).json()["entries"][0]
    r = client.delete(f"/api/availability/{saved['id']}")
    assert r.status_code == 200 and r.json()["moved"] == []  # blocks that moved stay where they went
    assert starts(client)[1] == at(2, 18)
    assert client.get("/api/availability").json()["entries"] == []
    assert client.delete(f"/api/availability/{saved['id']}").status_code == 404


def test_what_the_api_refuses(client):
    assert say(client, at(2, 18), at(2, 14)).status_code == 400  # ends before it starts
    assert say(client, at(2, 8), at(2, 9)).status_code == 400  # already over
    assert client.post("/api/availability", json={"entries": []}).status_code == 422
    assert client.post("/api/availability", json={"entries": [{"kind": "maybe", "start": at(2, 14).isoformat(), "end": at(2, 15).isoformat()}]}).status_code == 422
    assert client.get("/api/availability").json()["entries"] == []


def test_parse_needs_the_model_and_saves_nothing(client, model):
    replies, _ = model
    object.__setattr__(settings, "meta_api_key", "")
    assert client.post("/api/availability/parse", json={"text": "busy tonight"}).status_code == 503
    object.__setattr__(settings, "meta_api_key", "key")
    replies.append(reading(said("off", "2026-11-02", "17:00", "24:00", "dinner")))
    r = client.post("/api/availability/parse", json={"text": "busy tonight, dinner"})
    assert r.status_code == 200
    [entry] = r.json()["entries"]
    assert entry["kind"] == "off" and entry["note"] == "dinner" and entry["id"] is None
    assert client.get("/api/availability").json()["entries"] == []  # the page confirms before saving
    replies.append(MetaError("Meta API 500"))
    assert client.post("/api/availability/parse", json={"text": "x"}).status_code == 502
