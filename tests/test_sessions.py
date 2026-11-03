"""Saying more than when you can work: "I have math tutoring at 1 and I'll do all my STEM work
during it; schedule my humanities around it." The model is stubbed. Demo data, seeded Monday
Nov 2 at 10 AM: Problem Set 6 (Algebra, due Tue, 60 min) and Lab 4 (Biology, due Wed, 90 min) are
STEM; the reading response (30 min, due Fri) and the essay (90 min, due Sun) are English."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import availability, db, demo, meta_client, push
from app.config import settings
from app.main import app
from tests.conftest import at

LAB, QUIZ, PSET, READING, ESSAY = 9000201, 9000202, 9000203, 9000204, 9000205
SAID = "I have math tutoring at 1pm and I will work on all my STEM assignments during it. Can you schedule my humanities assignments for times around it?"


def tutoring_reading():
    return availability._Reading(
        spans=[availability._Span(kind="session", date="2026-11-02", start_time="13:00", end_time="14:00", note="math tutoring", assignment_ids=[PSET, LAB, 42])],
        plans=[availability._Plan(assignment_ids=[READING, ESSAY], date="2026-11-02", start_time="", end_time="")],
        assumed=["Tutoring had no end time, so I gave it an hour."],
        unclear="",
    )


@pytest.fixture
def model(monkeypatch):
    object.__setattr__(settings, "meta_api_key", "key")
    replies, prompts = [], []

    def fake(prompt, schema, **kw):
        prompts.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr(meta_client, "generate_structured", fake)
    return replies, prompts


@pytest.fixture
def client(conn):
    demo.seed(conn, now=at(2, 10))
    with TestClient(app) as c:
        set_clock(c, at(2, 10))
        yield c


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(push, "send_ping", lambda conn, title, body, url="/", tag="", extra=None, skip="": sent.append((title, body, url, extra)) or 1)
    return sent


def set_clock(client, when):
    r = client.post("/api/clock", json={"action": "set", "when": when.isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


def blocks_of(client, assignment_id):
    return [b for b in client.get("/api/state").json()["blocks"] if b["assignment_id"] == assignment_id]


def t(iso):
    return datetime.fromisoformat(iso)


# ---- reading it ---------------------------------------------------------------------------------


def test_the_model_sees_the_work_list_and_its_reading_comes_back_with_names(client, model):
    replies, prompts = model
    replies.append(tutoring_reading())
    r = client.post("/api/availability/parse", json={"text": SAID})
    assert r.status_code == 200, r.text
    body = r.json()
    # it was shown each open item with its id, class and due date
    assert f"{PSET} | Algebra II (demo) | Problem Set 6: Logarithms | problem set | due Tue 2026-11-03 23:59" in prompts[0]
    [session] = body["entries"]
    assert session["kind"] == "session" and session["assignment_ids"] == [PSET, LAB]  # 42 isn't on the list
    assert session["names"] == ["Problem Set 6: Logarithms", "Lab 4: Osmosis in Potato Cores"]
    [plan] = body["plans"]
    # "around it": that day's work hours, stretched back to cover the session
    assert (t(plan["start"]), t(plan["end"])) == (at(2, 13), at(2, 21))
    assert plan["names"] == ["Reading Response: Chapters 7-9", "Argument Essay: First Draft"]
    assert body["notes"] == ["Tutoring had no end time, so I gave it an hour."]
    assert client.get("/api/availability").json()["entries"] == []  # nothing saved yet


def test_a_session_with_no_work_on_the_list_is_time_off():
    reading = availability._Reading(
        spans=[availability._Span(kind="session", date="2026-11-02", start_time="13:00", end_time="14:00", note="tutoring", assignment_ids=[42])]
    )
    p = availability.proposal_from(reading, at(2, 10), {PSET})
    assert [(e.kind, e.assignment_ids) for e in p.entries] == [("off", [])]


# ---- doing it -----------------------------------------------------------------------------------


def adjust(client, model):
    replies, _ = model
    replies.append(tutoring_reading())
    proposal = client.post("/api/availability/parse", json={"text": SAID}).json()
    r = client.post("/api/adjust", json={k: proposal[k] for k in ("entries", "plans")})
    assert r.status_code == 200, r.text
    return r.json()


def test_stem_work_goes_into_tutoring_and_humanities_around_it(client, model):
    body = adjust(client, model)
    # the hour is shared by how long each usually takes (60 and 90 min), due soonest first
    [pset] = blocks_of(client, PSET)
    [lab] = blocks_of(client, LAB)
    assert (t(pset["start"]), t(pset["end"]), pset["session"]) == (at(2, 13), at(2, 13, 25), "math tutoring")
    assert (t(lab["start"]), t(lab["end"])) == (at(2, 13, 25), at(2, 14))
    assert pset["pinned"] and lab["pinned"]  # their 3 PM and 4 PM blocks are gone
    # the humanities go in the free time right after tutoring, soonest due first
    [reading] = blocks_of(client, READING)
    [essay] = blocks_of(client, ESSAY)
    assert (t(reading["start"]), t(essay["start"])) == (at(2, 14), at(2, 14, 30))
    assert {b["assignment_id"] for b in body["placed"]} == {PSET, LAB, READING, ESSAY}
    assert body["notes"] == [] and body["kept"] == []
    # nothing else may be planned inside the session
    quiz = [b for b in blocks_of(client, QUIZ) if b["status"] == "planned"]
    assert all(not (at(2, 13) < t(b["end"]) and t(b["start"]) < at(2, 14)) for b in quiz)


def test_tutoring_announces_itself_then_asks_what_got_finished(client, model, pushes):
    adjust(client, model)
    events = set_clock(client, at(2, 13))["events"]
    assert events["pinged"] == [] and len(events["sessions"]) == 1  # one notification, not one per block
    title, body, url, extra = pushes[-1]
    assert title == "math tutoring: time for Problem Set 6: Logarithms, Lab 4: Osmosis in Potato Cores"
    assert extra["kind"] == "session"
    assert set_clock(client, at(2, 13, 50))["events"]["missed"] == []  # session blocks are never "missed"

    events = set_clock(client, at(2, 14))["events"]
    [session_id] = events["reviews"]
    title, body, url, extra = pushes[-1]
    assert title == "math tutoring is over" and body.startswith("What did you finish?") and url == f"/?review={session_id}"
    assert extra["kind"] == "review" and extra["actions"][0]["post"] == f"/api/sessions/{session_id}/review"
    [review] = client.get("/api/state").json()["reviews"]
    assert review["note"] == "math tutoring" and [i["assignment_id"] for i in review["items"]] == [PSET, LAB]
    # the lab already has a new block, in case it isn't finished
    assert any(b["status"] == "planned" and t(b["start"]) >= at(2, 14) for b in blocks_of(client, LAB))

    r = client.post(f"/api/sessions/{session_id}/review", json={"finished": [PSET]})
    assert r.status_code == 200
    assert r.json()["finished"] == [PSET] and [u["id"] for u in r.json()["unfinished"]] == [LAB]
    state = client.get("/api/state").json()
    assert PSET in state["done_assignments"] and LAB not in state["done_assignments"]
    assert state["reviews"] == []
    answers = client.get("/api/stats").json()["answers"]
    assert answers["finished"] == 1 and answers["unfinished"] == 1


def test_finishing_everything_from_the_notification(client, model, pushes):
    adjust(client, model)
    set_clock(client, at(2, 14, 5))
    extra = pushes[-1][3]
    action = extra["actions"][0]
    assert client.post(action["post"], json=action["body"]).status_code == 200
    done = client.get("/api/state").json()["done_assignments"]
    assert PSET in done and LAB in done


def test_taking_back_a_session_before_it_starts_gives_its_work_back(client, model):
    body = adjust(client, model)
    session_id = body["entries"][0]["id"]
    assert client.delete(f"/api/availability/{session_id}").status_code == 200
    for aid in (PSET, LAB):
        [b] = blocks_of(client, aid)
        assert b["session"] is None and t(b["start"]) >= at(2, 13)


def test_saying_work_is_done(client, model):
    replies, _ = model
    replies.append(availability._Reading(spans=[], done_ids=[READING]))
    proposal = client.post("/api/availability/parse", json={"text": "I already finished the reading response"}).json()
    assert proposal["done"] == [{"id": READING, "name": "Reading Response: Chapters 7-9"}]
    r = client.post("/api/adjust", json={"done": [d["id"] for d in proposal["done"]]})
    assert r.json()["done"] == [{"id": READING, "name": "Reading Response: Chapters 7-9"}]
    assert READING in client.get("/api/state").json()["done_assignments"]
    assert client.post("/api/adjust", json={}).status_code == 400
