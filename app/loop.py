"""The start loop's jobs: sync (Canvas -> cards -> planned blocks) and the tick that fires pings,
marks missed starts and notifies unanswered misses again.

main.py runs these from a background task that sleeps until `seconds_to_next` says the next
start, miss or re-notify is due, so pings land on time; they're plain functions over a
connection so tests and the clock endpoint can call them directly. Every ping, answer and start
lands in the `pings` table, which is the real usage data for the demo.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from app import checkin, db, prep, push, scheduler, sync
from app.clock import as_local, clock
from app.config import settings
from app.models import Assignment, Block, Ping, PrepCard
from app.progress import Progress

log = logging.getLogger(__name__)

sync_lock = threading.Lock()  # one sync at a time (background loop vs. the Sync button)
progress = Progress()  # what the running sync is doing, for /api/state

GRACE = timedelta(minutes=15)  # scheduler.mark_missed's default: a ping unanswered this long is a miss
NAG_EVERY = timedelta(minutes=10)  # an unanswered miss is notified again this often,
NAG_MAX = 3  # at most this many more times
MIN_SLEEP = 0.25  # seconds: the background loop never spins faster than this


def canvas_configured() -> bool:
    return bool(settings.canvas_base_url and settings.canvas_token)


def load_clock(conn: sqlite3.Connection) -> None:
    """Restore the simulated clock, so a server restart (or --reload) mid-demo keeps the time."""
    value = db.kv_get(conn, "clock_offset")
    clock.offset = timedelta(seconds=float(value)) if value else timedelta(0)


def save_clock(conn: sqlite3.Connection) -> None:
    db.kv_set(conn, "clock_offset", str(clock.offset.total_seconds()))


def replan(conn: sqlite3.Connection, now: datetime) -> list[Block]:
    """Plan blocks for upcoming assignments that don't have a live one (tests: their practice
    sessions). Returns the new, saved blocks.

    A missed block is history, so an assignment whose block was missed gets a new one right away;
    the check-in then asks the student to keep that time or pick another. New blocks keep out of
    time the student said they can't work and may use time they said they can.
    """
    horizon = now + timedelta(days=settings.card_horizon_days)
    assignments = [a for a in db.list_assignments(conn) if a.due_at and a.due_at <= horizon]
    cards = db.list_cards(conn)
    _drop_long_blocks(conn, assignments, cards)
    existing = [b for b in db.list_blocks(conn) if b.status != "missed"]
    new = scheduler.plan_blocks(assignments, cards, now, existing, avail=db.list_availability(conn, now))
    for b in new:
        b.id = db.save_block(conn, b)
    return new


def _drop_long_blocks(conn: sqlite3.Connection, assignments: list[Assignment], cards: list[PrepCard]) -> None:
    """A test or quick task planned as one long work block (before its card said what it is, or
    before tests got practice sessions) loses that block if it was never pinged, so it gets its
    short ones."""
    by_card = {c.assignment_id: c for c in cards}
    longest: dict[int, timedelta] = {}
    for a in assignments:
        card = by_card.get(a.id)
        if scheduler.is_test(a, card) or (card and card.quick):
            longest[a.id] = timedelta(minutes=scheduler.block_minutes(a, card))
    for b in db.list_blocks(conn):
        if (
            b.assignment_id in longest
            and b.status == "planned"
            and b.end - b.start > longest[b.assignment_id]
            and db.latest_ping(conn, b.id) is None
        ):
            db.delete_block(conn, b.id)


def block_name(a: Assignment | None, card: PrepCard | None, assignment_id: int) -> str:
    """What a block is called: the assignment, or for a test "Practice for Quiz: …", so the week
    never reads as if the quiz itself were at that time."""
    if a is None:
        return f"Assignment {assignment_id}"
    return f"Practice for {a.name}" if scheduler.is_test(a, card) else a.name


def _name(conn: sqlite3.Connection, assignment_id: int) -> str:
    a = db.get_assignment(conn, assignment_id)
    return block_name(a, db.get_card(conn, assignment_id), assignment_id) if a else "your assignment"


def when(dt: datetime) -> str:
    """'Sat 3:00 PM' in the student's zone (no %-I: Windows strftime lacks it)."""
    t = as_local(dt)
    return f"{t:%a} {t.hour % 12 or 12}:{t:%M %p}"


def nag(conn: sqlite3.Connection, block_id: int) -> tuple[int, datetime] | None:
    """(notifications sent, last one) for a missed block, or None if this server never notified it."""
    value = db.kv_get(conn, f"nag.{block_id}")
    if not value:
        return None
    d = json.loads(value)
    return d["count"], datetime.fromisoformat(d["last"])


def _set_nag(conn: sqlite3.Connection, block_id: int, count: int, when: datetime) -> None:
    db.kv_set(conn, f"nag.{block_id}", json.dumps({"count": count, "last": when.isoformat()}))


def waiting_misses(conn: sqlite3.Connection, now: datetime) -> list[Block]:
    """Missed blocks the check-in is still asking about."""
    blocks = db.list_blocks(conn)
    assignments = {a.id: a for a in db.list_assignments(conn)}
    closed = checkin.closed(assignments, blocks, now)
    return [b for b in checkin.waiting(blocks, db.list_pings(conn), closed) if b.status == "missed"]


def buttons(conn: sqlite3.Connection, asked: Block, target: Block, now: datetime) -> list[dict]:
    """A notification's buttons, the same as the check-in's on the page (app.js startOptions):
    Start now, "In 15 min" and/or "Next work time" with day and time, Pick a time…. Each carries
    the check-in `answer` (and `picked_time`) sw.js posts; Pick a time opens the check-in. `target`
    is the block an answer moves (a miss's new block); when that already sits at the next work
    time, the button keeps it there."""
    a = db.get_assignment(conn, asked.assignment_id)
    out = [{"action": "start_now", "title": "Start now", "answer": "start_now"}]
    planned = target.start if target.id != asked.id else None
    avail = db.list_availability(conn, now)
    for c in checkin.choices(db.list_blocks(conn), target.id, now, a.due_at if a else None, planned=planned, avail=avail):
        if c["key"] == "in_15":
            out.append({"action": "in_15", "title": f"In 15 min · {when(c['start'])}", "answer": "plus_15"})
        else:
            keep = target.id != asked.id and c["start"] == target.start
            out.append(
                {
                    "action": "next",
                    "title": f"Next work time · {when(c['start'])}",
                    "answer": "keep" if keep else "pick_time",
                    "picked_time": None if keep else c["start"].isoformat(),
                }
            )
    out.append({"action": "pick", "title": "Pick a time…"})
    return out


def _notify_miss(conn: sqlite3.Connection, b: Block, again: Block | None, renotify: bool, now: datetime) -> None:
    push.send_ping(
        conn,
        f"Missed start: {_name(conn, b.assignment_id)}",
        f"Planned for {when(b.start)}. When will you start?",
        f"/?block={b.id}",
        tag=f"block-{b.id}",
        extra={"kind": "missed", "block_id": b.id, "renotify": renotify, "actions": buttons(conn, b, again or b, now)},
    )


def tick(conn: sqlite3.Connection, now: datetime) -> dict[str, list[int]]:
    """Mark missed starts and give each a new block, ping every block whose start time has
    arrived, then notify unanswered misses again (NAG_EVERY apart, NAG_MAX times; a miss whose new
    block was just pinged isn't waiting any more). Returns the ids of blocks pinged, missed, newly
    planned and notified again."""
    events: dict[str, list[int]] = {"pinged": [], "missed": [], "new": [], "nagged": []}
    missed = scheduler.mark_missed(db.list_blocks(conn), now)
    for b in missed:
        db.save_block(conn, b)
        _set_nag(conn, b.id, 1, now)
        events["missed"].append(b.id)
    new = replan(conn, now) if missed else []
    events["new"] = [b.id for b in new]
    for b in missed:
        _notify_miss(conn, b, next((n for n in new if n.assignment_id == b.assignment_id), None), False, now)
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
            extra={"kind": "ping", "block_id": b.id, "actions": buttons(conn, b, b, now)},
        )
        events["pinged"].append(b.id)
    blocks = db.list_blocks(conn)
    for b in waiting_misses(conn, now):
        sent = nag(conn, b.id)
        if sent and sent[0] <= NAG_MAX and now - sent[1] >= NAG_EVERY:
            _set_nag(conn, b.id, sent[0] + 1, now)
            _notify_miss(conn, b, checkin.replacement(blocks, b), True, now)
            events["nagged"].append(b.id)
    return events


def next_due(blocks: list[Block], now: datetime, nags: dict[int, tuple[int, datetime]]) -> datetime | None:
    """When tick next has work: a planned block's start (ping), a pinged block's grace running out
    (miss), or an unanswered miss's next re-notify (`nags`: block id -> (count, last))."""
    times = [b.start for b in blocks if b.status == "planned"]
    times += [b.start + GRACE for b in blocks if b.status == "pinged"]
    times += [last + NAG_EVERY for count, last in nags.values() if count <= NAG_MAX]
    return min(times) if times else None


def seconds_to_next(conn: sqlite3.Connection, now: datetime, cap: float) -> float:
    """How long the background loop may sleep: until just after `next_due`, at most `cap`. The
    simulated clock is real time plus a fixed offset, so a simulated wait is a real one."""
    nags = {b.id: sent for b in waiting_misses(conn, now) if (sent := nag(conn, b.id))}
    due = next_due(db.list_blocks(conn), now, nags)
    if due is None:
        return cap
    return min(cap, max(MIN_SLEEP, (due - now).total_seconds() + 0.05))


def run_sync() -> dict:
    """Canvas sync (if a token is set) -> cards for upcoming work -> plan blocks.

    Opens its own connection so it can run on a worker thread. Reports each phase to `progress`
    as it goes, and records the time, any error and the finished report (phase timings, cards
    that failed) in the kv table for the page to show.
    """
    if not sync_lock.acquire(blocking=False):
        return {"skipped": True}
    progress.reset()
    conn = db.connect()
    result: dict = {"assignments": 0, "cards": 0, "blocks": 0}
    try:
        if canvas_configured():
            result["assignments"] = sync.sync_all(conn, progress)
        result["cards"] = len(prep.build_all_missing(conn, clock.now(), progress=progress))
        progress.phase("planning")
        result["blocks"] = len(replan(conn, clock.now()))
        db.kv_set(conn, "sync_error", "")
    except Exception as e:
        log.exception("sync failed")
        result["error"] = f"{type(e).__name__}: {e}"[:300]
        db.kv_set(conn, "sync_error", result["error"])
    finally:
        report = progress.finish()
        result["timings"] = report["timings"]
        result["failures"] = report["failures"]
        log.info("sync done: %s", json.dumps({k: v for k, v in result.items() if k != "failures"}))
        db.kv_set(conn, "sync_report", json.dumps({**report, **result}))
        db.kv_set(conn, "last_sync", datetime.now(timezone.utc).isoformat())
        conn.close()
        sync_lock.release()
    return result
