"""The start loop's jobs: sync (Canvas -> cards -> planned blocks) and the tick that fires pings
and marks missed starts.

main.py runs these from a background task; they're plain functions over a connection so tests
and the clock endpoint can call them directly. Every ping, answer and start lands in the
`pings` table, which is the real usage data for the demo.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from app import db, prep, push, scheduler, sync
from app.clock import clock
from app.config import settings
from app.models import Block, Ping

log = logging.getLogger(__name__)

sync_lock = threading.Lock()  # one sync at a time (background loop vs. the Sync button)


def canvas_configured() -> bool:
    return bool(settings.canvas_base_url and settings.canvas_token)


def load_clock(conn: sqlite3.Connection) -> None:
    """Restore the simulated clock, so a server restart (or --reload) mid-demo keeps the time."""
    value = db.kv_get(conn, "clock_offset")
    clock.offset = timedelta(seconds=float(value)) if value else timedelta(0)


def save_clock(conn: sqlite3.Connection) -> None:
    db.kv_set(conn, "clock_offset", str(clock.offset.total_seconds()))


def replan(conn: sqlite3.Connection, now: datetime) -> list[Block]:
    """Plan blocks for upcoming assignments that don't have one. Returns the new, saved blocks."""
    horizon = now + timedelta(days=settings.card_horizon_days)
    assignments = [a for a in db.list_assignments(conn) if a.due_at and a.due_at <= horizon]
    new = scheduler.plan_blocks(assignments, db.list_cards(conn), now, db.list_blocks(conn))
    for b in new:
        b.id = db.save_block(conn, b)
    return new


def _name(conn: sqlite3.Connection, assignment_id: int) -> str:
    a = db.get_assignment(conn, assignment_id)
    return a.name if a else "your assignment"


def tick(conn: sqlite3.Connection, now: datetime) -> dict[str, list[int]]:
    """Mark missed starts, then ping every block whose start time has arrived."""
    events: dict[str, list[int]] = {"pinged": [], "missed": []}
    for b in scheduler.mark_missed(db.list_blocks(conn), now):
        db.save_block(conn, b)
        events["missed"].append(b.id)
        push.send_ping(
            conn,
            f"Missed start: {_name(conn, b.assignment_id)}",
            "No problem. When will you start? Tap to pick a time.",
            f"/?block={b.id}",
            tag=f"block-{b.id}",
        )
    while (b := scheduler.due_ping(db.list_blocks(conn), now)) is not None:
        b = b.model_copy(update={"status": "pinged"})
        db.save_block(conn, b)
        db.save_ping(conn, Ping(block_id=b.id, sent_at=now))
        card = db.get_card(conn, b.assignment_id)
        push.send_ping(
            conn,
            f"Time to start: {_name(conn, b.assignment_id)}",
            card.first_step if card else "Open your prep card.",
            f"/?block={b.id}",
            tag=f"block-{b.id}",
        )
        events["pinged"].append(b.id)
    return events


def run_sync() -> dict:
    """Canvas sync (if a token is set) -> cards for upcoming work -> plan blocks.

    Opens its own connection so it can run on a worker thread. Records the time and any error in
    the kv table for the page to show.
    """
    if not sync_lock.acquire(blocking=False):
        return {"skipped": True}
    conn = db.connect()
    result: dict = {"assignments": 0, "cards": 0, "blocks": 0}
    try:
        if canvas_configured():
            result["assignments"] = sync.sync_all(conn)
        result["cards"] = len(prep.build_all_missing(conn, clock.now()))
        result["blocks"] = len(replan(conn, clock.now()))
        db.kv_set(conn, "sync_error", "")
    except Exception as e:
        log.exception("sync failed")
        result["error"] = f"{type(e).__name__}: {e}"[:300]
        db.kv_set(conn, "sync_error", result["error"])
    finally:
        db.kv_set(conn, "last_sync", datetime.now(timezone.utc).isoformat())
        conn.close()
        sync_lock.release()
    return result
