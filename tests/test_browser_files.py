"""The Chrome extension's side of the server: which Google files it should fetch, what it may
upload, Google Assignments copies, failures, and the key. All data here is made up."""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import browser_files, db
from app.browser_files import UploadError
from app.main import app
from app.models import Assignment, Course, PrepCard, SourceFile
from tests.conftest import at

NOW = at(2, 10)
DOC = "https://docs.google.com/document/d/DOC_aaaaaaaaaaaaaaaaaaaa/edit?usp=sharing"
SLIDES = "https://docs.google.com/presentation/d/SLIDES_bbbbbbbbbbbbbbbbb/edit"
DRIVE = "https://drive.google.com/file/d/DRIVE_cccccccccccccccccc/view?usp=drive_link"
COPY = "https://docs.google.com/document/d/COPY_dddddddddddddddddddd/edit"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
CANVAS = "https://canvas.test"


def _link(url: str, label: str) -> str:
    return f'<p><a href="{url}">{label}</a></p>'


def _assignment(id: int, html: str, day: int = 5, **kw) -> Assignment:
    return Assignment(
        id=id, course_id=1, name=f"Assignment {id}", description_html=html, due_at=at(day, 23),
        html_url=f"{CANVAS}/courses/1/assignments/{id}", **kw,
    )


@pytest.fixture
def seeded(conn):
    db.save_course(conn, Course(id=1, name="Chemistry"))
    for a in [
        _assignment(1, _link(DOC, "Lab handout") + _link(SLIDES, "Notes")),
        _assignment(2, _link(DOC, "Same handout") + _link(DRIVE, "Data sheet")),
        _assignment(3, _link("https://docs.google.com/document/d/LATER_eeeeeeeeeeeeeeeeee/edit", "Later"), day=28),
        _assignment(4, _link("https://docs.google.com/document/d/DONE_ffffffffffffffffffff/edit", "Done"), submitted=True),
        _assignment(5, "<p>Open it from Canvas.</p>", external_tool_url="https://assignments.google.com/lti/a/1"),
    ]:
        db.save_assignment(conn, a)
    return conn


def _docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


def test_ref_reads_every_google_link_shape():
    assert browser_files.ref(DOC).fetch_url == "https://docs.google.com/document/d/DOC_aaaaaaaaaaaaaaaaaaaa/export?format=pdf"
    slides = browser_files.ref(SLIDES)
    assert slides.kind == "presentation" and slides.prefix == "google-slides"
    assert slides.fetch_url.endswith("/export/pdf")
    drive = browser_files.ref(DRIVE)
    assert drive.kind == "drive" and drive.fetch_url.startswith("https://drive.usercontent.google.com/download?id=DRIVE_")
    assert browser_files.ref("https://drive.google.com/open?id=DRIVE_cccccccccccccccccc").file_id == drive.file_id
    assert browser_files.ref("https://docs.google.com/document/u/1/d/DOC_aaaaaaaaaaaaaaaaaaaa/preview").file_id == "DOC_aaaaaaaaaaaaaaaaaaaa"
    assert browser_files.ref("https://docs.google.com/forms/d/e/FORM/viewform") is None
    assert browser_files.ref("https://example.com/handout.pdf") is None


def test_needed_lists_google_files_from_upcoming_work_once(seeded):
    got = browser_files.needed(seeded, NOW)
    by_id = {f["file_id"]: f for f in got["files"]}
    assert set(by_id) == {"DOC_aaaaaaaaaaaaaaaaaaaa", "SLIDES_bbbbbbbbbbbbbbbbb", "DRIVE_cccccccccccccccccc"}
    assert [a["id"] for a in by_id["DOC_aaaaaaaaaaaaaaaaaaaa"]["assignments"]] == [1, 2]
    assert by_id["DOC_aaaaaaaaaaaaaaaaaaaa"]["name"] == "Lab handout"
    assert got["copies"] == [{"assignment_id": 5, "name": "Assignment 5", "course": "Chemistry", "html_url": f"{CANVAS}/courses/1/assignments/5"}]


def test_needed_skips_files_a_card_already_read(seeded):
    card = PrepCard(assignment_id=1, kind="lab", summary="", first_step="", steps=[],
                    sources=[SourceFile(name="Notes", url=SLIDES, read=True)])
    db.save_card(seeded, card)
    ids = {f["file_id"] for f in browser_files.needed(seeded, NOW)["files"]}
    assert "SLIDES_bbbbbbbbbbbbbbbbb" not in ids
    assert "DOC_aaaaaaaaaaaaaaaaaaaa" in ids  # assignment 2 has no card yet


def test_upload_stores_the_file_under_its_google_id(seeded):
    got = browser_files.save_upload(seeded, DOC, PDF, "Lab handout.pdf", "Lab handout", now=NOW)
    assert got["new"] and got["assignments"] == [1, 2]
    s = browser_files.stored("https://docs.google.com/document/d/DOC_aaaaaaaaaaaaaaaaaaaa/preview")
    assert s.path.name == "google-DOC_aaaaaaaaaaaaaaaaaaaa.pdf" and s.path.read_bytes() == PDF and s.title == "Lab handout"
    assert "DOC_aaaaaaaaaaaaaaaaaaaa" not in {f["file_id"] for f in browser_files.needed(seeded, NOW)["files"]}

    slides = browser_files.save_upload(seeded, SLIDES, PDF, "Notes.pdf", now=NOW)
    assert browser_files.stored(SLIDES, seeded).path.name == f"google-slides-{slides['file_id']}.pdf"
    browser_files.save_upload(seeded, DRIVE, _docx(), "Data sheet.docx", now=NOW)
    assert browser_files.stored(DRIVE).path.suffix == ".docx"


def test_upload_refuses_pages_unknown_files_and_unasked_links(seeded):
    with pytest.raises(UploadError, match="web page"):
        browser_files.save_upload(seeded, DOC, b"<!DOCTYPE html><html>Sign in</html>", "export", now=NOW)
    with pytest.raises(UploadError, match="can read"):
        browser_files.save_upload(seeded, DOC, b"\x00\x01binary", "x.bin", now=NOW)
    with pytest.raises(UploadError, match="not a file linked"):
        browser_files.save_upload(seeded, "https://docs.google.com/document/d/OTHER_gggggggggggggggggg/edit", PDF, now=NOW)
    with pytest.raises(UploadError, match="not a file linked"):  # submitted work isn't asked for
        browser_files.save_upload(seeded, "https://docs.google.com/document/d/DONE_ffffffffffffffffffff/edit", PDF, now=NOW)
    with pytest.raises(UploadError, match="Google"):
        browser_files.save_upload(seeded, "https://example.com/a.pdf", PDF, now=NOW)
    assert browser_files.stored(DOC) is None


def test_same_bytes_again_are_not_new(seeded):
    assert browser_files.save_upload(seeded, DOC, PDF, now=NOW)["new"]
    again = browser_files.save_upload(seeded, DOC, PDF, now=NOW)
    assert not again["new"] and again["cards_marked"] == []
    changed = browser_files.save_upload(seeded, DOC, PDF + b"\n% edited\n", now=NOW)
    assert changed["new"]


def test_failures_rest_before_retrying_and_explain_themselves(seeded):
    browser_files.save_failure(seeded, DOC, "restricted", "HTTP 403", now=NOW)
    assert browser_files.reason(DOC) == browser_files.REASONS["restricted"]
    ids = lambda when: {f["file_id"] for f in browser_files.needed(seeded, when)["files"]}
    assert "DOC_aaaaaaaaaaaaaaaaaaaa" not in ids(at(2, 12))
    assert "DOC_aaaaaaaaaaaaaaaaaaaa" in ids(at(3, 11))  # a day later it's tried again

    browser_files.save_failure(seeded, SLIDES, "signin", now=NOW)
    assert "SLIDES_bbbbbbbbbbbbbbbbb" in ids(at(2, 11, 30))  # sign-in trouble retries within the hour
    with pytest.raises(UploadError):
        browser_files.save_failure(seeded, DOC, "because", now=NOW)


def test_a_failure_never_replaces_a_saved_file(seeded):
    browser_files.save_upload(seeded, DOC, PDF, now=NOW)
    assert browser_files.save_failure(seeded, DOC, "network", now=NOW) == {"file_id": "DOC_aaaaaaaaaaaaaaaaaaaa", "kept": "stored"}
    assert browser_files.stored(DOC) is not None and browser_files.reason(DOC) == ""


def test_opened_doc_from_a_google_assignment_is_the_students_copy(seeded):
    page = f"{CANVAS}/courses/1/assignments/5"
    got = browser_files.opened(seeded, COPY, page, NOW)
    assert got["wanted"] and got["assignment_id"] == 5 and got["fetch_url"].endswith("/export?format=pdf")

    assert not browser_files.opened(seeded, COPY, "https://evil.test/courses/1/assignments/5", NOW)["wanted"]
    assert not browser_files.opened(seeded, COPY, f"{CANVAS}/courses/1/assignments/1", NOW)["wanted"]  # not Google Assignments
    assert not browser_files.opened(seeded, COPY, "", NOW)["wanted"]
    with pytest.raises(UploadError):  # an assignment that doesn't use Google Assignments can't take a copy
        browser_files.save_upload(seeded, COPY, PDF, assignment_id=1, now=NOW)

    browser_files.save_upload(seeded, COPY, PDF, "Alex - Lab.pdf", "Alex - Lab", assignment_id=5, now=NOW)
    [copy] = browser_files.copies(5)
    assert copy.file_id == "COPY_dddddddddddddddddddd" and copy.title == "Alex - Lab"
    assert browser_files.needed(seeded, NOW)["copies"] == []
    other = "https://docs.google.com/document/d/OTHER_gggggggggggggggggg/edit"
    assert not browser_files.opened(seeded, other, page, NOW)["wanted"]  # one copy per assignment
    with pytest.raises(UploadError):
        browser_files.save_upload(seeded, other, PDF, assignment_id=5, now=NOW)


def test_opened_doc_linked_from_canvas_is_wanted_even_after_a_failure(seeded):
    browser_files.save_failure(seeded, DOC, "signin", now=NOW)
    got = browser_files.opened(seeded, DOC, "", NOW)
    assert got["wanted"] and got["assignment_id"] is None
    browser_files.save_upload(seeded, DOC, PDF, now=NOW)
    assert not browser_files.opened(seeded, DOC, "", NOW)["wanted"]


def test_key_is_made_once(conn):
    k = browser_files.key(conn)
    assert len(k) >= 30 and browser_files.key(conn) == k
    assert browser_files.key_ok(conn, k) and not browser_files.key_ok(conn, k + "x") and not browser_files.key_ok(conn, None)


# ---- over HTTP --------------------------------------------------------------------------------


@pytest.fixture
def client(seeded):
    with TestClient(app) as c:
        c.post("/api/clock", json={"action": "set", "when": NOW.isoformat()})
        yield c


def test_every_extension_call_needs_the_key(client, seeded):
    for method, path in [("get", "/api/ext/hello"), ("get", "/api/ext/needed"), ("post", "/api/ext/opened"),
                         ("post", "/api/ext/files"), ("post", "/api/ext/failed")]:
        assert getattr(client, method)(path).status_code == 401
    browser_files.key(seeded)
    assert client.get("/api/ext/needed", headers={"X-Start-Helper-Key": "wrong"}).status_code == 401


def test_extension_round_trip_over_http(client, seeded):
    h = {"X-Start-Helper-Key": browser_files.key(seeded)}
    assert client.get("/api/ext/hello", headers=h).json()["canvas_url"] == CANVAS
    files = client.get("/api/ext/needed", headers=h).json()["files"]
    doc = next(f for f in files if f["file_id"] == "DOC_aaaaaaaaaaaaaaaaaaaa")

    r = client.post("/api/ext/files", headers=h, data={"url": doc["url"], "title": "Lab handout"},
                    files={"file": ("Lab handout.pdf", PDF, "application/pdf")})
    assert r.status_code == 200 and r.json()["new"]
    r = client.post("/api/ext/files", headers=h, data={"url": doc["url"]},
                    files={"file": ("export", b"<html>Sign in</html>", "text/html")})
    assert r.status_code == 422 and "web page" in r.json()["detail"]

    r = client.post("/api/ext/failed", headers=h, json={"url": SLIDES, "reason": "restricted", "detail": "HTTP 403"})
    assert r.status_code == 200 and browser_files.reason(SLIDES) == browser_files.REASONS["restricted"]
    assert client.post("/api/ext/failed", headers=h, json={"url": SLIDES, "reason": "nope"}).status_code == 422

    r = client.post("/api/ext/opened", headers=h, json={"url": COPY, "opener_url": f"{CANVAS}/courses/1/assignments/5"})
    assert r.json()["wanted"] and r.json()["assignment_id"] == 5
    r = client.post("/api/ext/files", headers=h, data={"url": COPY, "assignment_id": "5"},
                    files={"file": ("copy.pdf", PDF, "application/pdf")})
    assert r.status_code == 200 and browser_files.copies(5)[0].url == COPY
