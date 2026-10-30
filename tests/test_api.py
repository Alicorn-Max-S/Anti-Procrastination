"""End-to-end through the HTTP API with demo data: ping, missed start, check-in, reflow."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import db, demo, push
from app.main import app
from tests.conftest import at


def set_clock(client, when):
    r = client.post("/api/clock", json={"action": "set", "when": when.isoformat()})
    assert r.status_code == 200
    return r.json()


@pytest.fixture
def client(conn):
    demo.seed(conn, now=at(2, 10))  # Monday morning
    with TestClient(app) as c:
        set_clock(c, at(2, 10))
        yield c


def test_pages_and_health(client):
    assert client.get("/").status_code == 200
    assert "showNotification" in client.get("/sw.js").text
    assert client.get("/api/health").json()["model"] is False


def test_cards_and_state(client):
    cards = client.get("/api/cards").json()
    assert len(cards) == 5 and cards[0]["name"] == "Problem Set 6: Logarithms"
    quiz = next(c for c in cards if c["kind"] == "test")
    assert quiz["practice"] and quiz["next_block"]
    state = client.get("/api/state").json()
    assert state["simulated"] and len(state["blocks"]) == 5 and state["checkin"] == []


def test_the_start_loop(client):
    first = min(client.get("/api/state").json()["blocks"], key=lambda b: b["start"])

    # 1. the clock reaches the block's start: it's pinged
    start = datetime.fromisoformat(first["start"])
    events = set_clock(client, start)["events"]
    assert events["pinged"] == [first["id"]]
    [pending] = client.get("/api/state").json()["checkin"]
    assert pending["status"] == "pinged" and pending["first_step"]

    # 2. nobody starts within 15 minutes: it's missed
    events = client.post("/api/clock", json={"action": "advance", "minutes": 20}).json()["events"]
    assert events["missed"] == [first["id"]]
    assert client.get("/api/state").json()["checkin"][0]["status"] == "missed"

    # 3. "after practice, around 7": the block moves and the check-in clears
    seven = start.replace(hour=19, minute=0)
    r = client.post(f"/api/blocks/{first['id']}/checkin", json={"answer": "pick_time", "picked_time": seven.isoformat()})
    assert r.status_code == 200
    assert datetime.fromisoformat(r.json()["block"]["start"]) == seven
    assert client.get("/api/state").json()["checkin"] == []

    # 4. at 7 it pings again, and "start now" records the start
    set_clock(client, seven)
    r = client.post(f"/api/blocks/{first['id']}/checkin", json={"answer": "start_now"})
    assert r.json()["block"]["status"] == "started"
    stats = client.get("/api/stats").json()
    assert stats["pings"] == 2 and stats["starts"] == 1 and stats["answers"]["pick_time"] == 1


def test_start_and_done_from_a_card(client):
    r = client.post("/api/assignments/9000204/start")
    assert r.status_code == 200 and r.json()["block"]["status"] == "started"
    assert client.post("/api/assignments/9000204/done").status_code == 200
    card = next(c for c in client.get("/api/cards").json() if c["assignment_id"] == 9000204)
    assert card["done"]


def test_dashboard(client):
    d = client.get("/api/dashboard", params={"course_id": demo.SPANISH}).json()
    assert d["weakest"][0]["topic"] == "preterite_vs_imperfect"
    assert len(d["rows"]) == 12


def test_upload_without_key_is_refused(client):
    r = client.post("/api/tests/upload", data={"course_id": demo.SPANISH}, files={"file": ("q.jpg", b"\xff\xd8", "image/jpeg")})
    assert r.status_code == 503
    r = client.post("/api/tests/upload", data={"course_id": demo.SPANISH}, files={"file": ("q.heic", b"x", "image/heic")})
    assert r.status_code == 400


def test_push_is_a_noop_without_keys(conn):
    assert push.send_ping(conn, "t", "b") == 0
    db.save_subscription(conn, {"endpoint": "https://push.test/1"})
    assert push.send_ping(conn, "t", "b") == 0


def test_vapid_keys_generate():
    public, private = push.generate_keys()
    assert len(public) == 87 and len(private) == 43
