"""Adding work by hand, on top of what syncs on its own: an old Canvas assignment that was never
finished, something from Modules that isn't an assignment, and the student's own task and files.
Canvas is faked; nothing here is added unless the student asks."""

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import canvas, db, demo, prep
from app.config import settings
from app.main import app
from app.models import Assignment
from tests.conftest import at

BIO = 9_000_001
HISTORY_DOC = 9_000_150  # a Biology assignment due last week, never turned in


def set_clock(client, when):
    r = client.post("/api/clock", json={"action": "set", "when": when.isoformat()})
    assert r.status_code == 200, r.text


@pytest.fixture
def client(conn):
    demo.seed(conn, now=at(2, 10))
    db.save_assignment(conn, Assignment(id=HISTORY_DOC, course_id=BIO, name="Cell Theory Doc", due_at=at(2, 10) - timedelta(days=6),
                                        description_html="<p>Fill out the cell theory doc.</p>", html_url="https://canvas.test/courses/1/assignments/150"))
    with TestClient(app) as c:
        set_clock(c, at(2, 10))
        yield c


def listed(client):
    return {c["assignment_id"]: c for c in client.get("/api/cards").json()}


def blocks_of(client, aid):
    return [b for b in client.get("/api/state").json()["blocks"] if b["assignment_id"] == aid]


# ---- an old Canvas assignment -----------------------------------------------------------------


def test_a_past_due_assignment_is_offered_not_added(client):
    assert HISTORY_DOC not in listed(client)  # sync and replanning never put it back on their own
    body = client.get("/api/added/candidates").json()
    [item] = [i for i in body["items"] if i["id"] == HISTORY_DOC]
    assert item["group"] == "past" and item["course"] == "AP Biology (demo)"
    assert datetime.fromisoformat(body["finish_by"]) == at(3, 23, 59)  # the end of tomorrow
    on_list = set(listed(client))
    assert not on_list & {i["id"] for i in body["items"]}  # nothing already listed is offered again


def test_adding_it_puts_it_on_the_list_with_a_block_before_the_finish_by_time(client):
    r = client.post("/api/added", json={"source": "canvas", "assignment_id": HISTORY_DOC})
    assert r.status_code == 200, r.text
    assert r.json()["next_block"] and datetime.fromisoformat(r.json()["next_block"]) < at(3, 23, 59)
    card = listed(client)[HISTORY_DOC]
    assert card["added"] and card["origin"] == "canvas" and not card["past_due"]
    assert datetime.fromisoformat(card["due_at"]) == at(3, 23, 59)
    assert datetime.fromisoformat(card["canvas_due_at"]) == at(2, 10) - timedelta(days=6)
    assert HISTORY_DOC not in {i["id"] for i in client.get("/api/added/candidates").json()["items"]}
    # it stays added through a sync that rewrites the row from Canvas
    conn = db.connect()
    db.save_assignment(conn, Assignment(id=HISTORY_DOC, course_id=BIO, name="Cell Theory Doc", due_at=at(2, 10) - timedelta(days=6)))
    conn.close()
    assert listed(client)[HISTORY_DOC]["added"]

    # a new finish-by time plans it again; one that has passed is refused
    r = client.put(f"/api/added/{HISTORY_DOC}", json={"finish_by": at(6, 23, 59).isoformat()})
    assert r.status_code == 200
    assert client.put(f"/api/added/{HISTORY_DOC}", json={"finish_by": at(1, 12).isoformat()}).status_code == 400
    [b] = [b for b in blocks_of(client, HISTORY_DOC) if b["status"] == "planned"]
    assert datetime.fromisoformat(b["end"]) <= at(6, 23, 59)

    # taking it off the list leaves it in Canvas and offers it again
    assert client.delete(f"/api/added/{HISTORY_DOC}").status_code == 200
    assert HISTORY_DOC not in listed(client) and blocks_of(client, HISTORY_DOC) == []
    assert HISTORY_DOC in {i["id"] for i in client.get("/api/added/candidates").json()["items"]}
    assert client.delete(f"/api/added/{HISTORY_DOC}").status_code == 404


# ---- something from Modules -------------------------------------------------------------------


@pytest.fixture
def fake_modules(monkeypatch):
    """Biology's modules: Week 9 lists its items; Week 10 makes us fetch them (Canvas may leave
    them out). Week 10 has a page, a file, an outside link, a heading, and the assignment."""
    week10 = [
        {"id": 501, "title": "Notes: Osmosis", "type": "Page", "url": "https://canvas.test/api/v1/courses/9000001/pages/osmosis", "html_url": "https://canvas.test/courses/9000001/modules/items/501"},
        {"id": 502, "title": "Osmosis worksheet.pdf", "type": "File", "content_id": 77, "url": "https://canvas.test/api/v1/courses/9000001/files/77", "html_url": "https://canvas.test/courses/9000001/modules/items/502"},
        {"id": 503, "title": "Membrane video", "type": "ExternalUrl", "external_url": "https://example.org/membranes", "html_url": "https://canvas.test/courses/9000001/modules/items/503"},
        {"id": 504, "title": "Thursday", "type": "SubHeader"},
        {"id": 505, "title": "Lab 4", "type": "Assignment", "content_id": 9_000_201, "html_url": "https://canvas.test/courses/9000001/modules/items/505"},
    ]
    by_id = {it["id"]: it for it in week10}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/courses/9000001/modules":
            return httpx.Response(200, json=[
                {"id": 9, "name": "Week 9", "items": [{"id": 401, "title": "Cell Theory Doc", "type": "Assignment", "content_id": HISTORY_DOC}]},
                {"id": 10, "name": "Week 10"},
            ])
        if path == "/api/v1/courses/9000001/modules/10/items":
            return httpx.Response(200, json=week10)
        if path.startswith("/api/v1/courses/9000001/modules/10/items/"):
            item = by_id.get(int(path.rsplit("/", 1)[-1]))
            return httpx.Response(200, json=item) if item else httpx.Response(404)
        if path == "/api/v1/courses/9000001/pages/osmosis":
            return httpx.Response(200, json={"title": "Notes: Osmosis", "body": "<p>Copy the osmosis notes and answer the 3 questions.</p>"})
        return httpx.Response(404)

    object.__setattr__(settings, "canvas_token", "tok")
    monkeypatch.setattr(canvas, "_client", lambda: httpx.Client(base_url="https://canvas.test", transport=httpx.MockTransport(handler)))


def test_modules_list_their_items_and_say_what_is_on_the_list(client, fake_modules):
    r = client.get(f"/api/canvas/modules?course_id={BIO}")
    assert r.status_code == 200, r.text
    week9, week10 = r.json()["modules"]
    assert week9["items"][0]["title"] == "Cell Theory Doc" and not week9["items"][0]["on_list"]
    items = {i["id"]: i for i in week10["items"]}  # fetched separately
    assert items[505]["on_list"]  # Lab 4 already syncs onto the list
    assert not items[504]["addable"] and items[501]["addable"]


def test_a_modules_page_becomes_work_with_its_own_card(client, fake_modules):
    r = client.post("/api/added", json={"source": "module", "course_id": BIO, "module_id": 10, "item_id": 501})
    assert r.status_code == 200, r.text
    aid = r.json()["assignment_id"]
    assert aid < 0  # never meets a Canvas id
    card = listed(client)[aid]
    assert card["origin"] == "module" and card["course"] == "AP Biology (demo)" and card["name"] == "Notes: Osmosis"
    assert "osmosis notes" in card["summary"]
    assert any(b["status"] == "planned" for b in blocks_of(client, aid))
    # the same item again is refused, and it now shows as on the list
    assert client.post("/api/added", json={"source": "module", "course_id": BIO, "module_id": 10, "item_id": 501}).status_code == 400
    week10 = client.get(f"/api/canvas/modules?course_id={BIO}").json()["modules"][1]
    assert next(i for i in week10["items"] if i["id"] == 501)["on_list"]
    # removing a Modules item deletes it
    assert client.delete(f"/api/added/{aid}").status_code == 200
    assert aid not in listed(client)


def test_a_modules_file_link_or_assignment(client, fake_modules, conn):
    f = client.post("/api/added", json={"source": "module", "course_id": BIO, "module_id": 10, "item_id": 502}).json()
    a = db.get_assignment(conn, f["assignment_id"])
    assert a.attachment_urls == ["https://canvas.test/api/v1/courses/9000001/files/77"]  # read like any Canvas file
    link = client.post("/api/added", json={"source": "module", "course_id": BIO, "module_id": 10, "item_id": 503}).json()
    assert [l.url for l in prep.known_links(db.get_assignment(conn, link["assignment_id"]))][0] == "https://example.org/membranes"
    # an item that is an assignment adds that assignment
    r = client.post("/api/added", json={"source": "module", "course_id": BIO, "module_id": 10, "item_id": 505})
    assert r.json()["assignment_id"] == 9_000_201
    assert client.post("/api/added", json={"source": "module", "course_id": BIO, "module_id": 10, "item_id": 504}).status_code == 400


def test_modules_need_canvas(client):
    assert client.get(f"/api/canvas/modules?course_id={BIO}").status_code == 503


# ---- the student's own ------------------------------------------------------------------------


def test_own_work_with_a_file(client, conn):
    r = client.post(
        "/api/added/own",
        data={"name": "Study for the driver's test", "course_id": "0", "instructions": "Chapters 3 and 4.\n\nThen the practice quiz.",
              "finish_by": at(5, 20).isoformat()},
        files=[("files", ("handbook notes.txt", b"Chapter 3: right of way. Chapter 4: signs.", "text/plain"))],
    )
    assert r.status_code == 200, r.text
    aid = r.json()["assignment_id"]
    card = listed(client)[aid]
    assert card["origin"] == "own" and card["course"] == "Your own" and card["first_step"] == "Open handbook notes.txt and read its first instruction."
    assert datetime.fromisoformat(card["due_at"]) == at(5, 20)
    a = db.get_assignment(conn, aid)
    from app import reader
    [doc] = reader.read_assignment(a)
    assert doc.readable and "right of way" in doc.text and doc.url == f"/api/own-files/{aid}/handbook%20notes.txt"
    got = client.get(doc.url)
    assert got.status_code == 200 and got.content.startswith(b"Chapter 3")
    assert client.get(f"/api/own-files/{aid}/..%2F..%2Ftest.db").status_code == 404

    assert client.post("/api/added/own", data={"name": "  "}).status_code == 400
    assert client.post("/api/added/own", data={"name": "x"}, files=[("files", ("run.exe", b"MZ", "application/octet-stream"))]).status_code == 400
    assert client.post("/api/added/own", data={"name": "x", "course_id": "12345"}).status_code == 400
    assert client.delete(f"/api/added/{aid}").status_code == 200
    assert aid not in listed(client)
