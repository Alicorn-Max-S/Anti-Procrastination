"""FastAPI app: JSON API, the static web page, and the background start loop.

Runs with `uvicorn app.main:app --reload`. /docs has the interactive API, which is also the
quickest way to hand-fix a bad test row. The background task syncs every SYNC_MINUTES and ticks
(pings + missed starts) right when the next one is due, at least every TICK_SECONDS; set
BACKGROUND=0 to turn it off.
"""

import asyncio
import json
import logging
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import browser_files, checkin, db, grader, loop, meta_client, prep, push, scheduler, student_model
from app.clock import clock, local_tz
from app.config import settings
from app.meta_client import MetaError
from app.models import Block, Ping, TestRow

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
TICK_SECONDS = 15  # the longest the background loop sleeps between ticks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


# ---- startup + background loop ----------------------------------------------------------------


def _tick_once() -> None:
    conn = db.connect()
    try:
        loop.tick(conn, clock.now())
    finally:
        conn.close()


_jobs: set[asyncio.Task] = set()  # keep references so running syncs aren't garbage-collected
_wake: asyncio.Event | None = None  # set to cut the background loop's sleep short
_wake_loop: asyncio.AbstractEventLoop | None = None


def wake_loop() -> None:
    """Make the background loop look again now: the clock moved or a block's time changed, so the
    next ping may be sooner than it planned for. Safe to call from any thread."""
    if _wake is not None and _wake_loop is not None and not _wake_loop.is_closed():
        _wake_loop.call_soon_threadsafe(_wake.set)


def _seconds_to_next(cap: float) -> float:
    conn = db.connect()
    try:
        return loop.seconds_to_next(conn, clock.now(), cap)
    finally:
        conn.close()


async def _sleep_until_next(cap: float = TICK_SECONDS) -> None:
    """Sleep until just after the next start, miss or re-notify (at most `cap`), or a wake_loop()."""
    global _wake, _wake_loop
    if _wake_loop is not asyncio.get_running_loop():
        _wake, _wake_loop = asyncio.Event(), asyncio.get_running_loop()
    _wake.clear()  # before measuring, so a wake during the measurement still counts
    delay = await asyncio.to_thread(_seconds_to_next, cap)
    try:
        await asyncio.wait_for(_wake.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass


async def _background() -> None:
    last_sync = float("-inf")
    while True:
        try:
            if time.monotonic() - last_sync >= settings.sync_minutes * 60 and not loop.sync_lock.locked():
                last_sync = time.monotonic()
                job = asyncio.create_task(asyncio.to_thread(loop.run_sync))
                _jobs.add(job)
                job.add_done_callback(_jobs.discard)
            await asyncio.to_thread(_tick_once)
            await _sleep_until_next()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background tick failed")
            await asyncio.sleep(TICK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    loop.load_clock(conn)
    conn.close()
    task = asyncio.create_task(_background()) if settings.background else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Start Helper", lifespan=lifespan)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


# ---- shared views -----------------------------------------------------------------------------


class Context:
    """Lookups the views need: assignments, cards and course names by id."""

    def __init__(self, conn) -> None:
        self.assignments = {a.id: a for a in db.list_assignments(conn)}
        self.cards = {c.assignment_id: c for c in db.list_cards(conn)}
        self.courses = {c.id: c.name for c in db.list_courses(conn)}

    def block(self, b: Block) -> dict:
        a = self.assignments.get(b.assignment_id)
        card = self.cards.get(b.assignment_id)
        return {
            **b.model_dump(mode="json"),
            "name": a.name if a else f"Assignment {b.assignment_id}",
            "kind": card.kind if card else a.kind if a else "other",
            "course": self.courses.get(a.course_id, "") if a else "",
            "due_at": a.due_at.isoformat() if a and a.due_at else None,
            "first_step": card.first_step if card else "",
        }


def _done_assignments(blocks: list[Block]) -> set[int]:
    return {b.assignment_id for b in blocks if b.status == "done"}


def _next_start(blocks: list[Block], now: datetime) -> Block | None:
    """The next planned block that hasn't started yet (the clock bar's "Jump to next start")."""
    ahead = [b for b in blocks if b.status == "planned" and b.start > now]
    return min(ahead, key=lambda b: b.start) if ahead else None


def _pending(conn, ctx: "Context", blocks: list[Block], now: datetime) -> list[Block]:
    return checkin.waiting(blocks, db.list_pings(conn), checkin.closed(ctx.assignments, blocks, now))


def _checkin_view(conn, ctx: "Context", blocks: list[Block], b: Block, now: datetime) -> dict:
    """A check-in entry, with the times "pick a time" offers (soonest first). A miss also carries
    the block that replaced it (its new planned time) and how many times it was notified."""
    view = ctx.block(b)
    target = b
    if b.status == "missed":
        r = checkin.replacement(blocks, b)
        view["replacement"] = {"id": r.id, "start": r.start.isoformat(), "at_risk": r.at_risk} if r else None
        sent = loop.nag(conn, b.id)
        view["notified"] = sent[0] if sent else 1
        target = r or b
    a = ctx.assignments.get(b.assignment_id)
    view["choices"] = [
        {"key": c["key"], "start": c["start"].isoformat()}
        for c in checkin.choices(blocks, target.id, now, a.due_at if a else None)
    ]
    return view


# ---- pages ------------------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    """Served from the root so the worker's scope covers the whole app."""
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


# ---- state + cards ----------------------------------------------------------------------------


@app.get("/api/health")
def health(conn=Depends(get_conn)) -> dict:
    return {
        "ok": True,
        "canvas": loop.canvas_configured(),
        "model": meta_client.configured(),
        "push": push.configured(conn),
        "push_devices": len(db.list_subscriptions(conn)),
        "push_error": json.loads(db.kv_get(conn, "push_error") or "null"),
        "model_usage": meta_client.usage,
    }


def _sync_state(conn) -> dict:
    """Sync status for the sync bar: live progress while running, the last report after."""
    running = loop.sync_lock.locked()
    report = db.kv_get(conn, "sync_report")
    return {
        "last": db.kv_get(conn, "last_sync"),
        "error": db.kv_get(conn, "sync_error") or None,
        "running": running,
        "progress": loop.progress.snapshot() if running else None,
        "report": json.loads(report) if report else None,
    }


@app.get("/api/state")
def state(conn=Depends(get_conn)) -> dict:
    """Everything the page polls for: clock, this week's blocks, pending check-ins, sync status."""
    now = clock.now()
    ctx = Context(conn)
    blocks = db.list_blocks(conn)
    day0 = now.astimezone(local_tz()).replace(hour=0, minute=0, second=0, microsecond=0)
    week = [b for b in blocks if day0 <= b.start < day0 + timedelta(days=7)]
    pending = _pending(conn, ctx, blocks, now)
    next_start = _next_start(blocks, now)
    return {
        "now": now.isoformat(),
        "simulated": clock.simulated,
        "week_start": day0.isoformat(),
        "blocks": [ctx.block(b) for b in week],
        "checkin": [_checkin_view(conn, ctx, blocks, b, now) for b in pending],
        "next_start": ctx.block(next_start) if next_start else None,
        "done_assignments": sorted(_done_assignments(blocks)),
        "sync": _sync_state(conn),
        "config": {
            "canvas": loop.canvas_configured(),
            "model": meta_client.configured(),
            "push": push.configured(conn),
            "push_key": push.keys(conn)[0],
            "push_devices": len(db.list_subscriptions(conn)),
        },
    }


@app.get("/api/cards")
def cards(conn=Depends(get_conn)) -> list[dict]:
    """Prep cards for work that's still open, joined with assignment details, soonest due first."""
    now = clock.now()
    ctx = Context(conn)
    blocks = db.list_blocks(conn)
    done = _done_assignments(blocks)
    next_block: dict[int, Block] = {}
    for b in blocks:
        ran = b.status == "started" and b.end <= now  # a session already over isn't "Planned for ..."
        if b.status in scheduler.ACTIVE and not ran and b.assignment_id not in next_block:
            next_block[b.assignment_id] = b
    missed = {  # start-loop: each assignment's unanswered missed start
        b.assignment_id: {"block_id": b.id, "start": b.start.isoformat()}
        for b in _pending(conn, ctx, blocks, now)
        if b.status == "missed"
    }
    out = []
    for card in ctx.cards.values():
        a = ctx.assignments.get(card.assignment_id)
        if a is None or a.submitted or (a.due_at and a.due_at < now - timedelta(days=2)):
            continue
        nb = next_block.get(a.id)
        out.append(
            {
                **card.model_dump(mode="json"),
                "name": a.name,
                "course": ctx.courses.get(a.course_id, ""),
                "due_at": a.due_at.isoformat() if a.due_at else None,
                "html_url": a.html_url,
                "course_id": a.course_id,
                "notes_medium": prep.notes_medium(conn, a.course_id),
                "done": a.id in done,
                "next_block": nb.start.isoformat() if nb else None,
                "missed": missed.get(a.id),
                "past_due": bool(a.due_at and a.due_at < now and a.id not in done),
            }
        )
    out.sort(key=lambda c: (c["done"], c["due_at"] is None, c["due_at"] or ""))
    return out


@app.post("/api/cards/{assignment_id}/regenerate")
def regenerate_card(assignment_id: int, conn=Depends(get_conn)) -> dict:
    a = db.get_assignment(conn, assignment_id)
    if a is None:
        raise HTTPException(404, "No such assignment")
    return prep.build_prep_card(conn, a).model_dump(mode="json")


@app.post("/api/sync")
def run_sync() -> dict:
    """Sync now (Canvas, then cards, then planning). Blocks until done; card generation is slow."""
    result = loop.run_sync()
    if result.get("skipped"):
        raise HTTPException(409, "A sync is already running")
    if result.get("error"):
        raise HTTPException(502, result["error"])
    return result


@app.post("/api/plan")
def plan(conn=Depends(get_conn)) -> dict:
    return {"new_blocks": [b.model_dump(mode="json") for b in loop.replan(conn, clock.now())]}


# ---- check-in ---------------------------------------------------------------------------------


class CheckinIn(BaseModel):
    answer: Literal["start_now", "plus_15", "pick_time", "keep"]
    picked_time: datetime | None = None
    endpoint: str = ""  # the answering device's push subscription; it clears its own notification


def _plan_answer(conn, block_id: int, answer: str, picked_time: datetime | None):
    """Work out an answer without saving it: (blocks before, blocks after, the Ping to record,
    the block that moves, ids of other blocks that moved). A miss that already has a new block
    (tick replans right away) moves that one instead, so the miss stays in the history."""
    now = clock.now()
    blocks = db.list_blocks(conn)
    asked = next((b for b in blocks if b.id == block_id), None)
    if asked is None:
        raise HTTPException(404, "No such block")
    target = (checkin.replacement(blocks, asked) if asked.status == "missed" else None) or asked
    if answer == "keep" and target.id == block_id:
        raise HTTPException(400, "There's no new time to keep: pick a time instead")
    due = {a.id: a.due_at for a in db.list_assignments(conn) if a.due_at}
    # The open ping this answers: the new block's if it was pinged, else the miss's. An answered
    # ping is never overwritten; the answer gets its own record on the block that was asked about.
    pings = (db.latest_ping(conn, target.id), db.latest_ping(conn, block_id))
    ping = next((p for p in pings if p and p.answer == "none"), None)
    try:
        new_blocks, record = checkin.apply_answer(blocks, target.id, answer, now, picked_time, ping, due)
    except ValueError as e:
        raise HTTPException(400, str(e))
    record = record if ping else record.model_copy(update={"block_id": block_id})
    before = {b.id: b for b in blocks}
    moved = [
        b.id
        for b in new_blocks
        if b.id != target.id and b.id in before and (before[b.id].start, before[b.id].end) != (b.start, b.end)
    ]
    return blocks, new_blocks, record, target, moved


def _answer(conn, block_id: int, answer: str, picked_time: datetime | None = None) -> dict:
    """Record the answer on the block that was pinged or missed, and reflow the week."""
    blocks, new_blocks, record, target, moved = _plan_answer(conn, block_id, answer, picked_time)
    before = {b.id: b for b in blocks}
    for b in new_blocks:
        if before.get(b.id) != b:
            db.save_block(conn, b)
    db.save_ping(conn, record)
    wake_loop()  # "+15 min" or a picked time may be sooner than the loop's next tick
    result = next(b for b in new_blocks if b.id == target.id)
    return {"block": Context(conn).block(result), "moved": moved}


def _answered_push(tags: list[str], body: str, skip: str) -> None:
    """Tell the student's other devices a check-in was answered, so sw.js swaps the ping or
    missed-start notification still showing there for a quiet one. Runs after the response."""
    conn = db.connect()
    try:
        push.send_ping(conn, "Start Helper", body, tag=tags[0], extra={"kind": "answered", "clear": tags}, skip=skip)
    finally:
        conn.close()


@app.post("/api/blocks/{block_id}/checkin")
def post_checkin(block_id: int, body: CheckinIn, background: BackgroundTasks, conn=Depends(get_conn)) -> dict:
    """The answer to a ping or a missed start. Returns the block and which other blocks moved."""
    result = _answer(conn, block_id, body.answer, body.picked_time)
    b = result["block"]
    when = loop.when(datetime.fromisoformat(b["start"]))
    text = f"Started: {b['name']}." if body.answer == "start_now" else f"{b['name']} is set for {when}."
    tags = list(dict.fromkeys([f"block-{block_id}", f"block-{b['id']}"]))
    background.add_task(_answered_push, tags, text, body.endpoint)
    return result


@app.post("/api/blocks/{block_id}/preview")
def preview_checkin(block_id: int, body: CheckinIn, conn=Depends(get_conn)) -> dict:
    """What an answer would do, without doing it: where the block goes and which blocks shift."""
    _, new_blocks, _, target, moved = _plan_answer(conn, block_id, body.answer, body.picked_time)
    ctx = Context(conn)
    by_id = {b.id: b for b in new_blocks}
    return {"block": ctx.block(by_id[target.id]), "moved": [ctx.block(by_id[i]) for i in moved]}


@app.post("/api/blocks/{block_id}/done")
def block_done(block_id: int, conn=Depends(get_conn)) -> dict:
    b = db.get_block(conn, block_id)
    if b is None:
        raise HTTPException(404, "No such block")
    db.save_block(conn, b.model_copy(update={"status": "done", "at_risk": False}))
    return {"ok": True}


@app.post("/api/assignments/{assignment_id}/start")
def start_assignment(assignment_id: int, conn=Depends(get_conn)) -> dict:
    """'Start now' from a card: uses the assignment's next block, or makes one if there isn't any."""
    a = db.get_assignment(conn, assignment_id)
    if a is None:
        raise HTTPException(404, "No such assignment")
    pending = sorted(
        (
            b for b in db.list_blocks(conn)
            if b.assignment_id == assignment_id and b.status in ("planned", "pinged", "missed")
        ),
        key=lambda b: (b.status == "missed", b.start),  # prefer a live block over an old miss
    )
    if pending:
        return _answer(conn, pending[0].id, "start_now")
    now = clock.now()
    card = db.get_card(conn, assignment_id)
    minutes = scheduler.DURATION_MINUTES[card.effort if card else "medium"]
    b = Block(assignment_id=assignment_id, start=now, end=now + timedelta(minutes=minutes), status="started")
    b.id = db.save_block(conn, b)
    db.save_ping(conn, Ping(block_id=b.id, sent_at=now, answer="start_now", started_at=now))
    return {"block": Context(conn).block(b), "moved": []}


@app.post("/api/assignments/{assignment_id}/done")
def assignment_done(assignment_id: int, conn=Depends(get_conn)) -> dict:
    """Mark an assignment finished (for paper work Canvas never sees submitted)."""
    if db.get_assignment(conn, assignment_id) is None:
        raise HTTPException(404, "No such assignment")
    live = [b for b in db.list_blocks(conn) if b.assignment_id == assignment_id and b.status in scheduler.ACTIVE]
    for b in live:
        db.save_block(conn, b.model_copy(update={"status": "done", "at_risk": False}))
    if not live:
        now = clock.now()
        db.save_block(conn, Block(assignment_id=assignment_id, start=now, end=now, status="done"))
    return {"ok": True}


@app.post("/api/assignments/{assignment_id}/undone")
def assignment_undone(assignment_id: int, conn=Depends(get_conn)) -> dict:
    """Undo "Mark done": its blocks go back to started / planned / missed, and the assignment gets
    a fresh planned block if none is live."""
    if db.get_assignment(conn, assignment_id) is None:
        raise HTTPException(404, "No such assignment")
    now = clock.now()
    started = {p.block_id for p in db.list_pings(conn) if p.started_at}
    changed, deleted = scheduler.undo_done(db.list_blocks(conn), assignment_id, now, started)
    if not changed and not deleted:
        raise HTTPException(409, "This assignment isn't marked done")
    for b in changed:
        db.save_block(conn, b)
    for block_id in deleted:
        db.delete_block(conn, block_id)
    new = loop.replan(conn, now)
    return {"restored": [b.id for b in changed], "new_blocks": [b.model_dump(mode="json") for b in new]}


class NotesIn(BaseModel):
    medium: Literal["paper", "doc"] | None = None


@app.put("/api/courses/{course_id}/notes")
def set_course_notes(course_id: int, body: NotesIn, conn=Depends(get_conn)) -> dict:
    """Where this course's notes are kept ("paper" / "doc" / null for not set). Cards built after
    this use it; existing cards change when regenerated."""
    prep.set_notes_medium(conn, course_id, body.medium)
    return {"course_id": course_id, "medium": body.medium}


# ---- simulated clock --------------------------------------------------------------------------


class ClockIn(BaseModel):
    action: Literal["set", "advance", "reset", "next"]
    when: datetime | None = None
    minutes: float | None = None


@app.get("/api/clock")
def get_clock() -> dict:
    return {"now": clock.now().isoformat(), "simulated": clock.simulated}


@app.post("/api/clock")
def set_clock(body: ClockIn, conn=Depends(get_conn)) -> dict:
    """Move the demo clock, then run the loop right away so a ping due at the new time fires now.

    "next" jumps to the next planned start. The reply says what happened, for the clock bar: the
    blocks pinged and missed (a miss names its new block), and the next start after the new time.
    """
    if body.action == "set":
        if body.when is None:
            raise HTTPException(400, "set needs `when`")
        clock.set(body.when)
    elif body.action == "advance":
        clock.advance(timedelta(minutes=body.minutes or 0))
    elif body.action == "next":
        target = _next_start(db.list_blocks(conn), clock.now())
        if target is None:
            raise HTTPException(409, "Nothing is planned after now. Sync, or set the clock by hand.")
        clock.set(target.start)
    else:
        clock.reset()
    loop.save_clock(conn)
    events = loop.tick(conn, clock.now())
    loop.replan(conn, clock.now())
    wake_loop()  # its sleep was measured on the old clock
    now = clock.now()
    blocks = db.list_blocks(conn)
    by_id = {b.id: b for b in blocks}
    ctx = Context(conn)
    next_start = _next_start(blocks, now)
    return {
        "now": now.isoformat(),
        "simulated": clock.simulated,
        "events": events,
        "pinged": [ctx.block(by_id[i]) for i in events["pinged"]],
        "missed": [_checkin_view(conn, ctx, blocks, by_id[i], now) for i in events["missed"]],
        "next_start": ctx.block(next_start) if next_start else None,
    }


# ---- push -------------------------------------------------------------------------------------


@app.get("/api/push/key")
def push_key(conn=Depends(get_conn)) -> dict:
    return {"key": push.keys(conn)[0], "configured": push.configured(conn)}


@app.post("/api/push/setup")
def push_setup(conn=Depends(get_conn)) -> dict:
    """The key the browser subscribes with. Makes a VAPID pair on first use when .env has none."""
    key = push.ensure_keys(conn)
    return {"key": key, "configured": bool(key), "devices": len(db.list_subscriptions(conn))}


@app.post("/api/push/subscribe")
def push_subscribe(subscription: dict, conn=Depends(get_conn)) -> dict:
    """Store this browser's subscription. The page sends it again on every load, so a swapped DB heals."""
    try:
        push.save_subscription(conn, subscription)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.kv_set(conn, "push_error", "")
    return {"ok": True, "devices": len(db.list_subscriptions(conn))}


class PushErrorIn(BaseModel):
    error: str
    browser: str = ""


@app.post("/api/push/error")
def push_error(body: PushErrorIn, conn=Depends(get_conn)) -> dict:
    """The page couldn't subscribe (the browser's push service refused, or similar). Kept so
    /api/health shows why push is off on a machine nobody is debugging live."""
    record = {"error": body.error[:300], "browser": body.browser[:200], "at": clock.real_now().isoformat()}
    db.kv_set(conn, "push_error", json.dumps(record))
    return {"ok": True}


@app.post("/api/push/test")
def push_test(conn=Depends(get_conn)) -> dict:
    """Push a test notification to every stored subscription. `sent` < `devices` means some failed."""
    sent = push.send_ping(conn, "Start Helper", "Push notifications work.", "/", tag="test")
    return {"sent": sent, "devices": len(db.list_subscriptions(conn))}


# ---- graded tests + dashboard -----------------------------------------------------------------


@app.get("/api/courses")
def courses(conn=Depends(get_conn)) -> list[dict]:
    return [{**c.model_dump(), "notes_medium": prep.notes_medium(conn, c.id)} for c in db.list_courses(conn)]


@app.get("/api/dashboard")
def dashboard(course_id: int, conn=Depends(get_conn)) -> dict:
    return {
        "course_id": course_id,
        "grid": student_model.grid_table(conn, course_id),
        "weakest": student_model.weakest_cells(conn, course_id, n=3),
        "evidence": student_model.feedback_evidence(conn, course_id, limit=8),
        "rows": [r.model_dump() for r in db.list_test_rows(conn, course_id)],
    }


@app.get("/api/stats")
def stats(conn=Depends(get_conn)) -> dict:
    """Usage numbers from the start loop: pings, answers, and how long starting took."""
    pings = db.list_pings(conn)
    delays = [(p.started_at - p.sent_at).total_seconds() / 60 for p in pings if p.started_at]
    return {
        "pings": len(pings),
        "answers": dict(Counter(p.answer for p in pings)),
        "starts": len(delays),
        "avg_minutes_to_start": round(sum(delays) / len(delays), 1) if delays else None,
        "blocks": dict(Counter(b.status for b in db.list_blocks(conn))),
    }


@app.post("/api/tests/upload")
def upload_test(course_id: int = Form(...), file: UploadFile = File(...), conn=Depends(get_conn)) -> dict:
    """Save a graded-test photo (or PDF scan) and extract one row per question."""
    name = Path(file.filename or "photo.jpg").name
    if Path(name).suffix.lower() not in meta_client.MIME:
        raise HTTPException(400, "Upload a JPG, PNG, WEBP or PDF")
    if not meta_client.configured():
        raise HTTPException(503, "Set META_API_KEY in .env to read test photos")
    folder = Path(settings.files_dir) / "tests" / str(course_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{clock.real_now():%Y%m%d-%H%M%S}-{re.sub(r'[^\w.\-]', '_', name)}"
    path.write_bytes(file.file.read())
    try:
        rows = grader.extract_rows(path, course_id, conn)
    except MetaError as e:
        raise HTTPException(502, str(e))
    ids = db.save_test_rows(conn, rows)
    return {"saved": len(ids), "rows": [r.model_copy(update={"id": i}).model_dump() for r, i in zip(rows, ids)]}


@app.put("/api/tests/rows/{row_id}")
def update_row(row_id: int, row: TestRow, conn=Depends(get_conn)) -> dict:
    db.update_test_row(conn, row.model_copy(update={"id": row_id}))
    return {"ok": True}


@app.delete("/api/tests/rows/{row_id}")
def delete_row(row_id: int, conn=Depends(get_conn)) -> dict:
    db.delete_test_row(conn, row_id)
    return {"ok": True}


# ---- Chrome extension (extension/) ------------------------------------------------------------
# The extension fetches school-only Google files in the student's signed-in Chrome and posts them
# here (browser_files.py). Every call carries the key from `python -m app.browser_files key`.


def extension_conn(x_start_helper_key: str | None = Header(None), conn=Depends(get_conn)):
    """The request's connection, once the extension's key checks out."""
    if not browser_files.key_ok(conn, x_start_helper_key):
        raise HTTPException(401, "Wrong or missing key: run `python -m app.browser_files key` and paste it into the extension")
    return conn


@app.get("/api/ext/hello")
def extension_hello(conn=Depends(extension_conn)) -> dict:
    """For the extension's options page: the key works, and which Canvas site to watch."""
    return {"ok": True, "canvas_url": settings.canvas_base_url, "max_bytes": browser_files.MAX_BYTES}


@app.get("/api/ext/needed")
def extension_needed(conn=Depends(extension_conn)) -> dict:
    return browser_files.needed(conn, clock.now())


class OpenedIn(BaseModel):
    url: str
    opener_url: str = ""  # the Canvas page the doc was opened from, when the extension can see it
    assignment_id: int | None = None  # picked in the popup: "this is my copy for ..."


@app.post("/api/ext/opened")
def extension_opened(body: OpenedIn, conn=Depends(extension_conn)) -> dict:
    """The student opened a Google doc: should the extension save it?"""
    return browser_files.opened(conn, body.url, body.opener_url, clock.now(), body.assignment_id)


@app.post("/api/ext/files")
def extension_upload(
    url: str = Form(...),
    title: str = Form(""),
    assignment_id: int | None = Form(None),
    via: str = Form("download"),  # "page": the doc's page view, because downloading was turned off
    file: UploadFile = File(...),
    conn=Depends(extension_conn),
) -> dict:
    data = file.file.read(browser_files.MAX_BYTES + 1)
    try:
        return browser_files.save_upload(
            conn, url, data, file.filename or "", title, assignment_id, clock.now(), via=via
        )
    except browser_files.UploadError as e:
        raise HTTPException(422, str(e))


class FailedIn(BaseModel):
    url: str
    reason: str  # a browser_files.REASONS key
    detail: str = ""


@app.post("/api/ext/failed")
def extension_failed(body: FailedIn, conn=Depends(extension_conn)) -> dict:
    try:
        return browser_files.save_failure(conn, body.url, body.reason, body.detail, clock.now())
    except browser_files.UploadError as e:
        raise HTTPException(422, str(e))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
