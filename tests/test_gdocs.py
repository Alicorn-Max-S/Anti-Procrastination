"""Google Docs/Drive links and Google Assignments: what gets fetched, and what the card says."""

import httpx
import pytest

from app import gdocs, meta_client, prep
from app.config import settings
from app.models import Assignment, Link

DOC = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit?usp=sharing"
DRIVE = "https://drive.google.com/file/d/1ZyXwVuTsRqPoNmLkJiHgFeDcBa/view"


def test_export_urls():
    assert gdocs.export_url(DOC) == "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/export?format=pdf"
    assert gdocs.export_url(DRIVE) == "https://drive.google.com/uc?export=download&id=1ZyXwVuTsRqPoNmLkJiHgFeDcBa"
    assert "presentation/d/abc/export/pdf" in gdocs.export_url("https://docs.google.com/presentation/d/abc/edit")
    assert gdocs.export_url("https://example.com/handout.pdf") is None


@pytest.fixture
def google(monkeypatch):
    """Doc links are public PDFs; Drive links bounce to a sign-in page, like a school-only file."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "docs.google.com" in request.url.host:
            return httpx.Response(200, content=b"%PDF-1.4 fake", headers={"content-type": "application/pdf"})
        return httpx.Response(200, content=b"<html>Sign in</html>", headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr(gdocs, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_download_public_and_private(google, tmp_path):
    path = gdocs.download(DOC, tmp_path)
    assert path is not None and path.suffix == ".pdf" and path.read_bytes().startswith(b"%PDF")
    assert gdocs.download(DRIVE, tmp_path) is None


def test_prompt_gets_public_file_and_names_private_one(google, monkeypatch, conn):
    a = Assignment(
        id=5, course_id=1, name="Homework Day 3", kind="problem_set", submission_types=["on_paper"],
        description_html=f'<p>Do the <a href="{DOC}">worksheet</a> and the <a href="{DRIVE}">problems</a>.</p>',
    )
    sent = {}
    object.__setattr__(settings, "meta_api_key", "key")

    def fake(prompt, schema, files=None, **kw):
        sent.update(prompt=prompt, files=files)
        return prep.CardDraft(kind="problem_set", summary="s", first_step="Open problems", steps=["a"],
                              links=[], watch_out="", effort="light")

    monkeypatch.setattr(meta_client, "generate_structured", fake)
    prep.build_prep_card(conn, a)
    assert [f.suffix for f in sent["files"]] == [".pdf"]
    assert "How it's turned in: on paper" in sent["prompt"]
    assert "can't read:\n- problems (shared only inside the school)" in sent["prompt"]


def test_google_assignments_card():
    a = Assignment(
        id=6, course_id=1, name="Quick write #2", submission_types=["external_tool"],
        external_tool_url="https://assignments.google.com/lti/a/xyz", html_url="https://canvas.test/a/6",
    )
    assert prep.uses_google_assignments(a)
    assert prep.turn_in(a).startswith("Google Assignments")
    assert prep.known_links(a) == [Link(label="Open in Google Assignments (via Canvas)", url="https://canvas.test/a/6")]


def test_google_assignments_fallback_first_step(conn):
    a = Assignment(id=7, course_id=1, name="Quick write #3", external_tool_url="https://assignments.google.com/lti/a/xyz")
    assert "Google Assignments" in prep.fallback_card(conn, a).first_step
