"""The Chrome extension's side of the server: which Google files it should fetch, what it may
upload, Google Assignments copies, failures, and the key. All data here is made up."""

import io
import zipfile

import httpx
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


def test_new_bytes_mark_cards_for_one_rebuild_and_same_bytes_never_do(seeded):
    db.save_card(seeded, PrepCard(assignment_id=1, kind="lab", summary="", first_step="", steps=[]))
    first = browser_files.save_upload(seeded, DOC, PDF, now=NOW)
    assert first["new"] and first["cards_marked"] == [1]  # assignment 2 has no card yet
    assert db.get_card(seeded, 1).stale

    db.save_card(seeded, db.get_card(seeded, 1).model_copy(update={"stale": False}))  # the sync rebuilt it
    again = browser_files.save_upload(seeded, DOC, PDF, now=NOW)
    assert not again["new"] and again["cards_marked"] == [] and not db.get_card(seeded, 1).stale
    changed = browser_files.save_upload(seeded, DOC, PDF + b"\n% edited\n", now=NOW)
    assert changed["new"] and changed["cards_marked"] == [1]


def test_failures_rest_before_retrying_and_explain_themselves(seeded):
    browser_files.save_failure(seeded, DOC, "restricted", "HTTP 403", now=NOW)
    assert browser_files.reason(DOC) == browser_files.REASONS["restricted"]
    ids = lambda when: {f["file_id"] for f in browser_files.needed(seeded, when)["files"]}
    assert "DOC_aaaaaaaaaaaaaaaaaaaa" not in ids(at(2, 12))
    assert "DOC_aaaaaaaaaaaaaaaaaaaa" in ids(at(3, 11))  # a day later it's tried again

    assert "DOC_aaaaaaaaaaaaaaaaaaaa" in browser_files.needed(seeded, at(2, 12))["watch"]  # still tried if opened
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


def test_a_copy_can_be_picked_by_hand_in_the_popup(seeded):
    got = browser_files.opened(seeded, COPY, "", NOW, assignment_id=5)
    assert got["wanted"] and got["assignment_id"] == 5
    assert not browser_files.opened(seeded, COPY, "", NOW, assignment_id=1)["wanted"]
    assert not browser_files.opened(seeded, COPY, "", NOW, assignment_id=999)["wanted"]


def test_opened_doc_linked_from_canvas_is_wanted_even_after_a_failure(seeded):
    browser_files.save_failure(seeded, DOC, "signin", now=NOW)
    got = browser_files.opened(seeded, DOC, "", NOW)
    assert got["wanted"] and got["assignment_id"] is None
    browser_files.save_upload(seeded, DOC, PDF, now=NOW)
    assert not browser_files.opened(seeded, DOC, "", NOW)["wanted"]


# What Google's page views look like, cut down: Docs' /mobilebasic is plain HTML, Slides'
# /htmlpresent draws each slide as SVG with a <text> per line.
DOC_PAGE = """<!DOCTYPE html><html><head><title>Lab handout - Google Docs</title>
<style>.c1{font-weight:700}</style><script>var x = "not text";</script></head>
<body><div class="doc-content"><p class="c1"><span>Titration lab</span></p>
<p><span>Fill the burette to 0.00&nbsp;mL.</span></p><ul><li>Record the start volume</li><li>Add 3 drops</li></ul>
<table><tr><td>Trial</td><td>Volume</td></tr><tr><td>1</td><td>12.4</td></tr></table></div></body></html>"""
SLIDES_PAGE = """<html><head><title>Notes - Google Slides</title></head><body>
<svg><g><text><tspan>Moles and mass</tspan></text><text><tspan>n = m / M</tspan></text></g></svg>
<svg><g><text><tspan>Practice: 18 g of water is how many moles?</tspan></text></g></svg></body></html>"""


def test_page_text_keeps_the_words_and_drops_the_page():
    title, text = browser_files.page_text(DOC_PAGE)
    assert title == "Lab handout"
    assert "not text" not in text and "font-weight" not in text
    assert text.splitlines()[:4] == ["Titration lab", "", "Fill the burette to 0.00 mL.", ""]
    assert "Record the start volume" in text and "1 12.4" in text
    title, text = browser_files.page_text(SLIDES_PAGE)
    assert title == "Notes"
    assert "Moles and mass\n\nn = m / M\n\nPractice: 18 g of water is how many moles?" == text


def test_a_page_view_is_kept_as_text_when_downloading_is_off(seeded):
    assert browser_files.ref(DOC).page_url == "https://docs.google.com/document/d/DOC_aaaaaaaaaaaaaaaaaaaa/mobilebasic"
    assert browser_files.ref(SLIDES).page_url.endswith("/htmlpresent") and browser_files.ref(DRIVE).page_url == ""
    db.save_card(seeded, PrepCard(assignment_id=1, kind="lab", summary="", first_step="", steps=[]))

    got = browser_files.save_upload(seeded, DOC, DOC_PAGE.encode(), "page-view.html", now=NOW, via="page")
    assert got["new"] and got["via"] == "page" and got["cards_marked"] == [1]
    s = browser_files.stored(DOC)
    assert s.via == "page" and s.title == "Lab handout" and s.path.name == "google-DOC_aaaaaaaaaaaaaaaaaaaa.txt"
    text = s.path.read_text(encoding="utf-8")
    assert text.startswith(f"Lab handout\n{browser_files.PAGE_VIEW_NOTE}\n\nTitration lab")

    # Downloading turned back on: the real file replaces the page text.
    browser_files.save_upload(seeded, DOC, PDF, "Lab handout.pdf", now=NOW)
    s = browser_files.stored(DOC)
    assert s.via == "download" and s.path.suffix == ".pdf" and not s.path.with_suffix(".txt").exists()


def test_a_page_view_needs_text_and_a_docs_slides_or_sheets_file(seeded):
    with pytest.raises(UploadError, match="no page view"):
        browser_files.save_upload(seeded, DRIVE, DOC_PAGE.encode(), now=NOW, via="page")
    with pytest.raises(UploadError, match="no text"):
        browser_files.save_upload(seeded, DOC, b"<html><title>Sign in</title><body> </body></html>", now=NOW, via="page")
    with pytest.raises(UploadError, match="web page"):  # a page sent as a download is still refused
        browser_files.save_upload(seeded, DOC, DOC_PAGE.encode(), "export", now=NOW)
    with pytest.raises(UploadError):
        browser_files.save_upload(seeded, DOC, DOC_PAGE.encode(), now=NOW, via="scrape")
    assert browser_files.stored(DOC) is None


def test_an_older_table_gets_the_via_column(conn, monkeypatch):
    monkeypatch.setattr(browser_files, "_ready", set())
    conn.execute("DROP TABLE IF EXISTS browser_files")
    conn.execute(
        "CREATE TABLE browser_files (file_id TEXT PRIMARY KEY, url TEXT NOT NULL, assignment_id INTEGER, path TEXT, "
        "title TEXT NOT NULL DEFAULT '', sha256 TEXT, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', "
        "detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO browser_files VALUES ('DOC_aaaaaaaaaaaaaaaaaaaa', ?, NULL, NULL, '', NULL, 'failed', "
                 "'signin', '', ?)", (DOC, NOW.isoformat()))
    conn.commit()
    assert browser_files.reason(DOC, conn) == browser_files.REASONS["signin"]
    assert {r["name"] for r in conn.execute("PRAGMA table_info(browser_files)")} >= {"via"}


def test_the_reader_reads_a_page_view(seeded, monkeypatch):
    from app import gdocs, reader

    monkeypatch.setattr(gdocs, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    browser_files.save_upload(seeded, SLIDES, SLIDES_PAGE.encode(), now=NOW, via="page")
    docs = {d.url: d for d in reader.read_assignment(db.get_assignment(seeded, 1))}
    assert docs[SLIDES].readable and "n = m / M" in docs[SLIDES].text and browser_files.PAGE_VIEW_NOTE in docs[SLIDES].text


def test_the_reader_uses_uploads_and_copies(seeded, monkeypatch):
    """Across lanes: ingest's reader reads what the extension stored, without fetching Google."""
    from app import gdocs, reader

    school_only = httpx.MockTransport(lambda r: httpx.Response(401))  # what Google tells the server
    monkeypatch.setattr(gdocs, "_client", lambda: httpx.Client(transport=school_only))
    browser_files.save_failure(seeded, SLIDES, "restricted", now=NOW)
    browser_files.save_upload(seeded, DOC, PDF, "Lab handout.pdf", "Lab handout", now=NOW)
    docs = {d.url: d for d in reader.read_assignment(db.get_assignment(seeded, 1))}
    assert docs[DOC].readable and docs[DOC].path.endswith("google-DOC_aaaaaaaaaaaaaaaaaaaa.pdf")
    assert not docs[SLIDES].readable and docs[SLIDES].reason == browser_files.REASONS["restricted"]

    browser_files.save_upload(seeded, COPY, PDF, "Alex - Lab.pdf", "Alex - Lab", assignment_id=5, now=NOW)
    [copy] = reader.read_assignment(db.get_assignment(seeded, 5))
    assert copy.url == COPY and copy.readable


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

    page = {"file": ("page-view.html", SLIDES_PAGE.encode(), "text/html")}
    r = client.post("/api/ext/files", headers=h, data={"url": SLIDES, "via": "page"}, files=page)
    assert r.status_code == 200 and r.json()["via"] == "page" and browser_files.stored(SLIDES).via == "page"
    r = client.post("/api/ext/files", headers=h, data={"url": DRIVE, "via": "page"}, files=page)
    assert r.status_code == 422 and "no page view" in r.json()["detail"]

    r = client.post("/api/ext/opened", headers=h, json={"url": COPY, "opener_url": f"{CANVAS}/courses/1/assignments/5"})
    assert r.json()["wanted"] and r.json()["assignment_id"] == 5
    r = client.post("/api/ext/files", headers=h, data={"url": COPY, "assignment_id": "5"},
                    files={"file": ("copy.pdf", PDF, "application/pdf")})
    assert r.status_code == 200 and browser_files.copies(5)[0].url == COPY
