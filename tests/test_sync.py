"""Canvas -> DB sync against a fake Canvas: parallel fetches, progress, no refetching, throttling,
and card builds reporting failures."""

import threading

import httpx
import pytest

from app import canvas, db, meta_client, prep, sync
from app.config import settings
from app.meta_client import MetaError
from app.models import Assignment
from app.progress import Progress
from tests.conftest import at
from tests.test_canvas import ASSIGNMENT_JSON


@pytest.fixture
def fake_canvas(monkeypatch):
    """Biology has one graded lab; Spanish's assignment list is broken (500)."""
    calls: list[str] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            calls.append(request.url.path)
        path = request.url.path
        if path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": 1, "name": "Biology"}, {"id": 3, "name": "Spanish"}])
        if path == "/api/v1/courses/1/assignments":
            return httpx.Response(200, json=[ASSIGNMENT_JSON, {**ASSIGNMENT_JSON, "id": 12, "name": "Lab 5", "submission": {}}])
        if path.endswith("/submissions/self"):
            return httpx.Response(200, json={"score": 15, "submission_comments": [
                {"id": 100, "author_name": "Ms. R", "comment": "More detail", "created_at": None}]})
        return httpx.Response(500)

    object.__setattr__(settings, "canvas_token", "tok")
    monkeypatch.setattr(canvas, "_client", lambda: httpx.Client(
        base_url="https://canvas.test", transport=httpx.MockTransport(handler)))
    return calls


def test_sync_saves_reports_and_skips_a_broken_course(conn, fake_canvas):
    progress = Progress()
    assert sync.sync_all(conn, progress) == 2
    report = progress.finish()
    assert set(report["timings"]) >= {"courses", "assignments", "feedback", "total"}
    assert [f["name"] for f in report["failures"]] == ["Spanish"]
    lab = db.get_assignment(conn, 11)
    assert lab.comments[0].text == "More detail"
    assert {c.name for c in db.list_courses(conn)} == {"Biology", "Spanish"}


def test_second_sync_doesnt_refetch_feedback(conn, fake_canvas):
    sync.sync_all(conn)
    first = sum(p.endswith("/submissions/self") for p in fake_canvas)
    sync.sync_all(conn)
    assert first == 1 and sum(p.endswith("/submissions/self") for p in fake_canvas) == 1
    assert db.get_assignment(conn, 11).comments[0].text == "More detail"  # kept from the first sync


def test_throttled_requests_are_retried(monkeypatch):
    replies = [httpx.Response(403, text="403 Forbidden (Rate Limit Exceeded)"), httpx.Response(200, json=[])]
    monkeypatch.setattr(canvas.time, "sleep", lambda s: None)
    client = httpx.Client(base_url="https://canvas.test", transport=httpx.MockTransport(lambda r: replies.pop(0)))
    assert canvas._get(client, "/api/v1/courses").status_code == 200


def test_card_failures_show_in_progress(conn, monkeypatch):
    object.__setattr__(settings, "meta_api_key", "key")
    now = at(2, 10)
    for i, name in [(1, "Lab 4"), (2, "Lab 5")]:
        db.save_assignment(conn, Assignment(id=i, course_id=1, name=name, kind="lab", due_at=at(3 + i, 23)))

    def fake(prompt, schema, **kw):
        if "Lab 5" in prompt:
            raise MetaError("Meta API 429: slow down")
        return prep.CardDraft(kind="lab", summary="s", first_step="Open it", steps=["a"], links=[],
                              watch_out="", effort="light")

    monkeypatch.setattr(meta_client, "generate_structured", fake)
    progress = Progress()
    cards = prep.build_all_missing(conn, now, progress=progress)
    assert [c.assignment_id for c in cards] == [1, 2]
    [failure] = progress.snapshot()["failures"]
    assert failure["name"] == "Lab 5" and "429" in failure["error"]
    assert progress.snapshot()["done"] == 2


def test_stale_cards_are_rebuilt(conn):
    from app.models import PrepCard

    now = at(2, 10)
    db.save_assignment(conn, Assignment(id=1, course_id=1, name="Lab 4", kind="lab", due_at=at(4, 23)))
    db.save_assignment(conn, Assignment(id=2, course_id=1, name="Lab 5", kind="lab", due_at=at(5, 23)))
    db.save_card(conn, PrepCard(assignment_id=1, kind="lab", summary="old", first_step="x", steps=[], stale=True))
    db.save_card(conn, PrepCard(assignment_id=2, kind="lab", summary="fresh", first_step="x", steps=[]))
    assert [c.assignment_id for c in prep.build_all_missing(conn, now)] == [1]
    assert not db.get_card(conn, 1).stale and db.get_card(conn, 2).summary == "fresh"
