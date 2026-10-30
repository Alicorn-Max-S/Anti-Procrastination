"""The start loop through the HTTP API: what a clock change reports, misses that get a new time
and keep asking (and notify again), past-due cards, the "pick a time" preview, push keys, and
that the background loop pings on time. Demo data, seeded Monday Nov 2 at 10 AM:
block 1 Problem Set 6 Mon 3 PM (due Tue night), block 2 Lab 4 Mon 4 PM (due Wed night),
block 3 the quiz Tue 3 PM (due Thu 9 AM)."""

import asyncio
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db, demo, loop, main, push
from app.clock import clock as sim_clock
from app.main import app
from app.models import Assignment, Block
from tests.conftest import at

PSET, LAB, QUIZ = 9000203, 9000201, 9000202


def clock(client, **body) -> dict:
    r = client.post("/api/clock", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def set_clock(client, when) -> dict:
    return clock(client, action="set", when=when.isoformat())


def state(client) -> dict:
    return client.get("/api/state").json()


def card(client, assignment_id) -> dict:
    return next(c for c in client.get("/api/cards").json() if c["assignment_id"] == assignment_id)


def close(iso: str, when: datetime) -> bool:
    """The simulated clock keeps ticking after a jump, so compare to the second."""
    return abs(datetime.fromisoformat(iso) - when) < timedelta(seconds=5)


@pytest.fixture
def client(conn):
    demo.seed(conn, now=at(2, 10))
    with TestClient(app) as c:
        set_clock(c, at(2, 10))
        yield c


def test_a_quiet_clock_change_names_the_next_start(client):
    # Max's case: 4:30 AM instead of PM. Nothing fires, and the reply says what's next.
    r = set_clock(client, at(2, 4, 30))
    assert r["events"] == {"pinged": [], "missed": [], "new": [], "nagged": []}
    assert r["next_start"]["id"] == 1 and r["next_start"]["name"] == "Problem Set 6: Logarithms"
    assert state(client)["next_start"]["id"] == 1


def test_jump_to_next_start_pings_it(client):
    r = clock(client, action="next")
    assert close(r["now"], at(2, 15))
    assert r["events"]["pinged"] == [1] and r["pinged"][0]["first_step"]
    assert r["next_start"]["id"] == 2


def test_jump_with_nothing_planned_says_so(conn):
    with TestClient(app) as c:
        r = c.post("/api/clock", json={"action": "next"})
    assert r.status_code == 409


def test_a_miss_gets_a_new_time_and_asks_about_it(client):
    set_clock(client, at(2, 15))
    r = clock(client, action="advance", minutes=20)  # no start within the 15-minute grace
    assert r["events"]["missed"] == [1] and len(r["events"]["new"]) == 1
    [miss] = r["missed"]
    new_id = r["events"]["new"][0]
    assert miss["replacement"]["id"] == new_id
    new_start = datetime.fromisoformat(miss["replacement"]["start"])
    assert new_start > at(2, 15, 20)

    # the check-in asks, with the new time, and so does the card
    [pending] = state(client)["checkin"]
    assert pending["id"] == 1 and pending["status"] == "missed" and pending["replacement"]["id"] == new_id
    c = card(client, PSET)
    assert c["missed"] == {"block_id": 1, "start": at(2, 15).isoformat()} and not c["past_due"]
    assert datetime.fromisoformat(c["next_block"]) == new_start

    # "keep": nothing moves, both clear, and the answer is recorded
    r = client.post("/api/blocks/1/checkin", json={"answer": "keep"})
    assert r.status_code == 200 and r.json()["block"]["id"] == new_id
    assert datetime.fromisoformat(r.json()["block"]["start"]) == new_start
    assert state(client)["checkin"] == [] and card(client, PSET)["missed"] is None
    assert client.get("/api/stats").json()["answers"]["keep"] == 1


def test_answering_a_miss_moves_its_new_block_and_keeps_the_miss(client):
    set_clock(client, at(2, 15))
    new_id = clock(client, action="advance", minutes=20)["events"]["new"][0]
    seven = at(2, 19)
    r = client.post("/api/blocks/1/checkin", json={"answer": "pick_time", "picked_time": seven.isoformat()})
    assert r.json()["block"]["id"] == new_id and datetime.fromisoformat(r.json()["block"]["start"]) == seven
    statuses = {b["id"]: b["status"] for b in state(client)["blocks"]}
    assert statuses[1] == "missed" and statuses[new_id] == "planned"  # the miss stays in the week
    assert state(client)["checkin"] == []

    # at 7 the new block pings; "start now" on the old miss's link still lands on it
    assert set_clock(client, seven)["events"]["pinged"] == [new_id]
    r = client.post("/api/blocks/1/checkin", json={"answer": "start_now"})
    assert r.json()["block"]["id"] == new_id and r.json()["block"]["status"] == "started"
    stats = client.get("/api/stats").json()
    assert stats["pings"] == 2 and stats["starts"] == 1


def test_keep_needs_a_new_time(client):
    set_clock(client, at(2, 15))
    r = client.post("/api/blocks/1/checkin", json={"answer": "keep"})  # pinged, not missed
    assert r.status_code == 400


def test_a_big_jump_still_asks_and_replans(client):
    # Monday morning to Wednesday noon: blocks 1-3 were never pinged
    r = set_clock(client, at(4, 12))
    assert sorted(r["events"]["missed"]) == [1, 2, 3]
    pending = {b["assignment_id"]: b for b in state(client)["checkin"]}

    # Problem Set 6 was due Tuesday night: no new block, its card says past due instead of asking
    assert PSET not in pending
    c = card(client, PSET)
    assert c["past_due"] and c["missed"] is None and c["next_block"] is None

    # Lab 4 and the quiz each got a new block today, and both the check-in and the card ask
    for aid in (LAB, QUIZ):
        assert pending[aid]["status"] == "missed"
        assert datetime.fromisoformat(pending[aid]["replacement"]["start"]) > at(4, 12)
        assert card(client, aid)["missed"]["block_id"] == pending[aid]["id"]
    assert len(r["events"]["new"]) == 2


def test_a_moved_block_missed_again_asks_again(client):
    # answered once ("pick 7 PM"), then 7 PM passes unpinged (server off): that's a new miss
    set_clock(client, at(2, 15))
    client.post("/api/blocks/1/checkin", json={"answer": "pick_time", "picked_time": at(2, 19).isoformat()})
    assert state(client)["checkin"] == []
    set_clock(client, at(2, 20, 30))
    [pending] = [b for b in state(client)["checkin"] if b["assignment_id"] == PSET]
    assert pending["id"] == 1 and pending["status"] == "missed" and pending["replacement"]


def test_marked_done_cards_are_not_past_due(client):
    client.post(f"/api/assignments/{PSET}/done")
    set_clock(client, at(4, 12))
    c = card(client, PSET)
    assert c["done"] and not c["past_due"]


def test_push_keys_are_made_once_when_asked(conn):
    assert not push.configured(conn) and push.keys(conn) == ("", "")
    key = push.ensure_keys(conn)
    assert len(key) == 87 and push.configured(conn) and push.ensure_keys(conn) == key
    assert push.send_ping(conn, "t", "b") == 0  # no subscriptions, so nothing goes out


def test_push_setup_endpoint(client):
    assert state(client)["config"]["push"] is False
    r = client.post("/api/push/setup").json()
    assert r["configured"] and r["devices"] == 0
    cfg = state(client)["config"]
    assert cfg["push"] and cfg["push_key"] == r["key"] and cfg["push_devices"] == 0


# ---- notifying an unanswered miss again ----------------------------------------------------------


def test_an_unanswered_miss_notifies_again_three_times_then_stops(client):
    set_clock(client, at(2, 15))
    clock(client, action="advance", minutes=20)  # block 1 missed at 3:20
    nagged = []
    for _ in range(5):
        nagged.append(clock(client, action="advance", minutes=10)["events"]["nagged"])
    assert nagged == [[1], [1], [1], [], []]
    [pending] = [b for b in state(client)["checkin"] if b["id"] == 1]
    assert pending["notified"] == 4


def test_answering_stops_the_reminders(client):
    set_clock(client, at(2, 15))
    clock(client, action="advance", minutes=20)
    client.post("/api/blocks/1/checkin", json={"answer": "keep"})
    assert clock(client, action="advance", minutes=10)["events"]["nagged"] == []


def test_no_reminder_once_the_new_time_arrives(client):
    set_clock(client, at(2, 15))
    clock(client, action="advance", minutes=20)  # block 1 missed at 3:20
    [pending] = [b for b in state(client)["checkin"] if b["id"] == 1]
    again = pending["replacement"]
    events = set_clock(client, datetime.fromisoformat(again["start"]))["events"]
    assert again["id"] in events["pinged"] and 1 not in events["nagged"]  # the ping asks instead


def test_answering_clears_the_notification_on_other_devices(client, monkeypatch):
    set_clock(client, at(2, 15))
    clock(client, action="advance", minutes=20)  # block 1 missed; block 3 replaces it
    sent = []
    monkeypatch.setattr(push, "send_ping", lambda conn, title, body, **kw: sent.append((body, kw)))
    r = client.post("/api/blocks/1/checkin", json={"answer": "keep", "endpoint": "https://push.test/laptop"})
    new_id = r.json()["block"]["id"]
    [(body, kw)] = sent
    assert kw["extra"] == {"kind": "answered", "clear": ["block-1", f"block-{new_id}"]}
    assert kw["skip"] == "https://push.test/laptop"  # the device that answered clears its own
    assert "is set for" in body


# ---- "pick a time": suggestions and preview --------------------------------------------------------


def test_checkin_offers_the_soonest_time_first(client):
    set_clock(client, at(2, 15, 20))  # block 1 (3 PM) is pinged; block 2 runs 4:00-5:30
    [pending] = state(client)["checkin"]
    choices = {c["key"]: datetime.fromisoformat(c["start"]) for c in pending["choices"]}
    assert pending["choices"][0]["key"] == "soonest"
    assert choices["soonest"] == at(2, 17, 30)  # 3:45 would run into block 2, so right after it
    assert choices["in_30"] == at(2, 16)


def test_preview_says_what_moves_without_saving(client):
    set_clock(client, at(2, 15, 20))
    before = {b["id"]: b["start"] for b in state(client)["blocks"]}
    r = client.post("/api/blocks/1/preview", json={"answer": "pick_time", "picked_time": at(2, 16).isoformat()})
    assert r.status_code == 200
    body = r.json()
    assert datetime.fromisoformat(body["block"]["start"]) == at(2, 16)
    [shifted] = body["moved"]
    assert shifted["id"] == 2 and datetime.fromisoformat(shifted["start"]) == at(2, 17)
    assert {b["id"]: b["start"] for b in state(client)["blocks"]} == before  # nothing saved


# ---- push diagnostics --------------------------------------------------------------------------------


def test_the_page_can_report_why_push_failed(client):
    client.post("/api/push/error", json={"error": "AbortError: Registration failed - push service error", "browser": "Chrome"})
    assert client.get("/api/health").json()["push_error"]["error"].startswith("AbortError")
    client.post("/api/push/subscribe", json={"endpoint": "https://push.test/x"})
    assert client.get("/api/health").json()["push_error"] is None


# ---- the background loop pings on time ---------------------------------------------------------------


def test_next_due_covers_starts_misses_and_reminders():
    now = at(2, 15)
    blocks = [
        Block(id=1, assignment_id=1, start=at(2, 16), end=at(2, 17)),
        Block(id=2, assignment_id=2, start=at(2, 14, 50), end=at(2, 15, 50), status="pinged"),
    ]
    assert loop.next_due(blocks, now, {}) == at(2, 15, 5)  # block 2's grace runs out first
    assert loop.next_due(blocks, now, {9: (2, at(2, 14, 52))}) == at(2, 15, 2)  # a reminder is due sooner
    assert loop.next_due(blocks, now, {9: (4, at(2, 14, 52))}) == at(2, 15, 5)  # reminders used up
    assert loop.next_due([], now, {}) is None


def test_background_loop_sleeps_until_the_start_then_pings_it(conn, monkeypatch):
    """No real waiting: the simulated clock is frozen and the sleep only records how long the loop
    asked to wait, so a busy machine can't make this fail."""
    now = at(2, 15)  # days away from real time, so a delay worked out from real time would be the cap
    monkeypatch.setattr(sim_clock, "now", lambda: now)
    start = now + timedelta(seconds=1.2)
    db.save_assignment(conn, Assignment(id=1, course_id=1, name="Timing", due_at=start + timedelta(days=1)))
    b = Block(assignment_id=1, start=start, end=start + timedelta(hours=1))
    b.id = db.save_block(conn, b)
    asked = []

    async def fake_wait_for(aw, timeout):
        aw.close()
        asked.append(timeout)
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    asyncio.run(main._sleep_until_next(15))
    assert asked == [pytest.approx(1.25)]  # until just after the start, not the 15 s cap
    now += timedelta(seconds=asked[0])  # the loop wakes when it asked to
    main._tick_once()
    ping = db.latest_ping(conn, b.id)
    assert ping is not None and ping.sent_at - b.start == timedelta(seconds=0.05)


def test_a_clock_change_wakes_the_sleeping_loop(conn):
    async def sleep_then_measure():
        began = time.monotonic()
        threading.Timer(0.2, main.wake_loop).start()  # what POST /api/clock does, from a worker thread
        await main._sleep_until_next(60)  # nothing planned, so only a wake ends this early
        return time.monotonic() - began

    assert asyncio.run(sleep_then_measure()) < 30  # woken, not timed out; loose so a busy machine passes
