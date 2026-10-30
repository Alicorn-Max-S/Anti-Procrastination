"""Round-trip tests for the DB layer: what goes in comes back unchanged."""

from datetime import datetime

from app import db
from app.models import Assignment, Block, Ping, PrepCard, TestRow
from tests.conftest import at


def test_round_trip(conn):
    a = Assignment(id=1, course_id=9, name="Fake Lab", due_at=datetime(2026, 11, 2, 23, 59))
    db.save_assignment(conn, a)
    card = PrepCard(assignment_id=1, kind="lab", summary="s", first_step="open the doc", steps=["a"])
    db.save_card(conn, card)

    assert db.get_assignment(conn, 1) == a
    assert db.list_cards(conn) == [card]
    assert db.get_card(conn, 1) == card


def test_blocks_and_pings(conn):
    b = Block(assignment_id=1, start=at(2, 15), end=at(2, 16), at_risk=True)
    b.id = db.save_block(conn, b)
    assert db.get_block(conn, b.id) == b

    b2 = b.model_copy(update={"status": "pinged", "at_risk": False})
    db.save_block(conn, b2)
    assert db.list_blocks(conn) == [b2]

    p = Ping(block_id=b.id, sent_at=at(2, 15))
    p.id = db.save_ping(conn, p)
    answered = p.model_copy(update={"answer": "start_now", "started_at": at(2, 15, 3)})
    db.save_ping(conn, answered)
    assert db.latest_ping(conn, b.id) == answered


def test_test_rows(conn):
    rows = [
        TestRow(course_id=5, source="q1.jpg", question="1. x", marks=1, marks_possible=2, topic="t", qtype="short_answer"),
        TestRow(course_id=6, source="q2.jpg", question="2. y"),
    ]
    ids = db.save_test_rows(conn, rows)
    got = db.list_test_rows(conn, 5)
    assert [r.id for r in got] == [ids[0]]
    assert got[0].marks == 1

    db.update_test_row(conn, got[0].model_copy(update={"topic": "fixed"}))
    assert db.list_test_rows(conn, 5)[0].topic == "fixed"
    db.delete_test_row(conn, ids[0])
    assert db.list_test_rows(conn, 5) == []


def test_old_database_gets_new_columns(conn):
    conn.executescript("DROP TABLE blocks; CREATE TABLE blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "assignment_id INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, "
                       "status TEXT NOT NULL DEFAULT 'planned');")
    db.init_db(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(blocks)")}
    assert "at_risk" in cols


def test_kv_and_subscriptions(conn):
    db.kv_set(conn, "k", "v")
    assert db.kv_get(conn, "k") == "v"
    assert db.kv_get(conn, "missing", "d") == "d"

    sub = {"endpoint": "https://push.test/1", "keys": {"p256dh": "x", "auth": "y"}}
    db.save_subscription(conn, sub)
    assert db.list_subscriptions(conn) == [sub]
    db.delete_subscription(conn, sub["endpoint"])
    assert db.list_subscriptions(conn) == []
