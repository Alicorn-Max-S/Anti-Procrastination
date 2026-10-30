"""The start loop through the HTTP API: what a clock change reports, misses that get a new time
and keep asking, past-due cards, and push keys. Demo data, seeded Monday Nov 2 at 10 AM:
block 1 Problem Set 6 Mon 3 PM (due Tue night), block 2 Lab 4 Mon 4 PM (due Wed night),
block 3 the quiz Tue 3 PM (due Thu 9 AM)."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import demo, push
from app.main import app
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
    assert r["events"] == {"pinged": [], "missed": [], "new": []}
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
