"""Starting stats: how late starts come by weekday, and the "you usually start late on Tuesdays" line."""

from datetime import timedelta

from fastapi.testclient import TestClient

from app import db, demo, habits
from app.main import app
from app.models import Block, Ping
from tests.conftest import at

MIN = timedelta(minutes=1)
TUESDAY_LINE = "You usually start 25 min late on Tuesdays."


def block(id: int, assignment_id: int, start, status="done") -> Block:
    return Block(id=id, assignment_id=assignment_id, start=start, end=start + 45 * MIN, status=status)


def tuesdays(*late: float) -> list[habits.Start]:
    """Starts planned for 4 PM on Nov 3 (a Tuesday) and the Tuesdays before it."""
    return [habits.Start(at(3, 16) - timedelta(weeks=i), m) for i, m in enumerate(late)]


def test_a_snoozed_start_counts_from_the_first_ping():
    blocks = [block(1, 10, at(3, 16))]
    pings = [
        Ping(block_id=1, sent_at=at(3, 16), answer="plus_15"),
        Ping(block_id=1, sent_at=at(3, 16, 15), answer="start_now", started_at=at(3, 16, 22)),
    ]
    assert habits.starts(pings, blocks) == [habits.Start(at(3, 16), 22)]


def test_a_missed_start_moved_to_the_evening_counts_from_the_first_time():
    blocks = [block(1, 10, at(5, 16), "missed"), block(2, 10, at(5, 19))]
    pings = [
        Ping(block_id=1, sent_at=at(5, 16), answer="pick_time", picked_time=at(5, 19)),
        Ping(block_id=2, sent_at=at(5, 19), answer="start_now", started_at=at(5, 19, 5)),
    ]
    assert habits.starts(pings, blocks) == [habits.Start(at(5, 16), 185)]


def test_each_assignment_and_each_start_is_its_own_try():
    blocks = [block(1, 10, at(2, 16)), block(2, 20, at(2, 16, 15)), block(3, 10, at(4, 16))]
    pings = [
        Ping(block_id=1, sent_at=at(2, 16), answer="start_now", started_at=at(2, 16, 30)),
        Ping(block_id=2, sent_at=at(2, 16, 15), answer="start_now", started_at=at(2, 16, 17)),
        Ping(block_id=3, sent_at=at(4, 16), answer="start_now", started_at=at(4, 16, 1)),
        Ping(block_id=99, sent_at=at(4, 18), answer="start_now", started_at=at(4, 18)),  # its block was deleted
    ]
    assert [s.late for s in habits.starts(pings, blocks)] == [30, 2, 1]


def test_the_line_needs_three_late_starts_on_that_weekday():
    assert habits.line(tuesdays(22, 31), 1) is None
    assert habits.line(tuesdays(22, 31, 25), 1) == TUESDAY_LINE
    assert habits.line(tuesdays(2, 31, 5), 1) is None  # usually on time
    assert habits.line(tuesdays(22, 31, 25), 2) is None  # nothing on Wednesdays
    assert habits.lines(tuesdays(22, 31, 25) + [habits.Start(at(4, 16), 0)]) == [TUESDAY_LINE]


def test_late_text():
    assert habits.late_text(25.4) == "25 min"
    assert habits.late_text(185) == "about 3 hours"
    assert habits.late_text(2 * 24 * 60 + 100) == "about 2 days"


def test_weekday_rows_count_starts_and_misses():
    rows = habits.weekdays(tuesdays(22, 31, 25), [block(1, 10, at(3, 16), "missed")])
    assert rows[0] == {"day": "Monday", "starts": 0, "median_late": None, "missed": 0}
    assert rows[1] == {"day": "Tuesday", "starts": 3, "median_late": 25, "missed": 1}
    assert len(rows) == 7


def test_demo_usage_gives_the_tuesday_line_and_leaves_the_week_alone(conn):
    demo.seed(conn, now=at(5, 10))
    planned = [(b.assignment_id, b.start) for b in db.list_blocks(conn)]
    demo.seed(conn, now=at(5, 10), usage=True)
    assert [(b.assignment_id, b.start) for b in db.list_blocks(conn) if b.status == "planned"] == planned
    with TestClient(app) as c:
        c.post("/api/clock", json={"action": "set", "when": at(5, 10).isoformat()})
        assert c.get("/api/state").json()["checkin"] == []
        s = c.get("/api/stats").json()
    assert s["habits"] == [TUESDAY_LINE]
    tuesday = next(r for r in s["weekdays"] if r["day"] == "Tuesday")
    assert tuesday == {"day": "Tuesday", "starts": 3, "median_late": 25, "missed": 0}
    assert sum(r["missed"] for r in s["weekdays"]) == 1  # the Thursday moved to 7 PM


def test_a_tuesday_ping_says_so(conn):
    demo.seed(conn, now=at(3, 10), usage=True)
    with TestClient(app) as c:
        c.post("/api/clock", json={"action": "set", "when": at(3, 10).isoformat()})
        r = c.post("/api/clock", json={"action": "next"}).json()
        assert r["events"]["pinged"]
        pinged = [b for b in c.get("/api/state").json()["checkin"] if b["status"] == "pinged"]
    assert pinged and pinged[0]["start"].startswith("2026-11-03")
    assert pinged[0]["habit"] == TUESDAY_LINE
