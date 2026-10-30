"""Unit materials and practice sets with Canvas, Google and the model all faked."""

import httpx
import pytest

from app import canvas, db, gdocs, materials, meta_client, practice
from app.config import settings
from app.materials import Material
from app.models import Assignment, PrepCard, SubmissionComment
from tests.conftest import at

COURSE = 7
TEST = Assignment(id=12, course_id=COURSE, name="Test 2: Vectors & Relative Motion", kind="test", due_at=at(9, 8),
                  description_html="<p>Covers vectors and relative motion.</p>", html_url="https://canvas.test/a/12")
HW = Assignment(
    id=11, course_id=COURSE, name="Vectors HW", kind="problem_set", due_at=at(4, 23), html_url="https://canvas.test/a/11",
    description_html='<p><iframe title="Vectors HW doc" src="https://docs.google.com/document/d/BBBBBBBBBBBBBBBBBBBBBBBB/preview"></iframe></p>',
    comments=[SubmissionComment(id=1, assignment_id=11, author="T", text="Include the angle with every resultant.")],
)
EARLIER = Assignment(id=10, course_id=COURSE, name="Kinematics Lab", kind="lab", due_at=at(1, 23), html_url="https://canvas.test/a/10")
MODULES = [
    {"id": 1, "name": "Unit 1: Kinematics", "items": [{"id": 9, "type": "Assignment", "title": "Kinematics Lab", "content_id": 10}]},
    {"id": 2, "name": "Unit 2: Vectors", "items": [
        {"id": 20, "type": "SubHeader", "title": "Notes"},
        {"id": 21, "type": "File", "title": "Presentation: Vectors", "content_id": 501,
         "url": "https://canvas.test/api/v1/courses/7/files/501", "html_url": "https://canvas.test/items/21"},
        {"id": 22, "type": "Page", "title": "KEY: Unit Vectors", "page_url": "key-unit-vectors", "html_url": "https://canvas.test/items/22"},
        {"id": 23, "type": "Assignment", "title": "Vectors HW", "content_id": 11, "html_url": "https://canvas.test/items/23"},
        {"id": 24, "type": "Page", "title": "Extra Practice", "page_url": "extra", "html_url": "https://canvas.test/items/24"},
        {"id": 25, "type": "Assignment", "title": TEST.name, "content_id": 12},
    ]},
]
DOCS = {
    "AAAAAAAAAAAAAAAAAAAAAAAA": "1. Find the unit vector in the direction of <3, 4>.\n\n2. Write <6, 8> in i, j form.",
    "BBBBBBBBBBBBBBBBBBBBBBBB": "1. A boat crosses a river at 4 m/s while the current flows at 3 m/s. Find its relative velocity.",
}


@pytest.fixture
def canvas_api(monkeypatch):
    """A fake Canvas with MODULES, two pages and one text file. Returns the list of paths hit."""
    object.__setattr__(settings, "canvas_token", "token")
    hits: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        hits.append(path)
        if path == f"/api/v1/courses/{COURSE}/modules":
            return httpx.Response(200, json=MODULES)
        if path.endswith("/pages/key-unit-vectors"):
            return httpx.Response(200, json={"body": '<p><iframe title="Unit Vectors Key" src="https://docs.google.com/document/d/AAAAAAAAAAAAAAAAAAAAAAAA/preview"></iframe></p>'})
        if path.endswith("/pages/extra"):
            return httpx.Response(200, json={"body": '<iframe src="https://docs.google.com/document/d/CCCCCCCCCCCCCCCCCCCCCCCC/preview"></iframe>'})
        if path.endswith("/files/501"):
            return httpx.Response(200, json={"id": 501, "size": 50, "display_name": "vectors.txt", "url": "https://canvas.test/dl/501"})
        if path == "/dl/501":
            return httpx.Response(200, text="Vectors have magnitude and direction.\n\nAdd vectors tip to tail.")
        return httpx.Response(404)

    monkeypatch.setattr(canvas, "_client", lambda: httpx.Client(base_url="https://canvas.test", transport=httpx.MockTransport(handle)))
    return hits


@pytest.fixture
def google(monkeypatch):
    """Google docs in DOCS export as text; any other doc answers with a sign-in page."""
    hits: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        for doc_id, text in DOCS.items():
            if doc_id in str(request.url):
                return httpx.Response(200, text="﻿" + text, headers={"content-type": "text/plain; charset=utf-8"})
        return httpx.Response(200, text="<html>Sign in</html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(gdocs, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    return hits


@pytest.fixture
def model(monkeypatch):
    object.__setattr__(settings, "meta_api_key", "key")
    replies, prompts = [], []

    def fake(prompt, schema, **kw):
        prompts.append((prompt, schema, kw))
        return replies.pop(0)

    monkeypatch.setattr(meta_client, "generate_structured", fake)
    return replies, prompts


@pytest.fixture
def course(conn):
    for a in (TEST, HW, EARLIER):
        db.save_assignment(conn, a)
    return conn


# ---- finding the unit -------------------------------------------------------------------------


def test_embedded_docs_reads_iframes():
    html = '<p><iframe style="w" title="Lab &amp; Notes" src="https://docs.google.com/document/d/X/preview" loading="lazy"></iframe></p>'
    [doc] = materials.embedded_docs(html)
    assert doc.label == "Lab & Notes" and doc.url == "https://docs.google.com/document/d/X/preview"


def test_unit_is_the_module_holding_the_test_and_is_cached(course, canvas_api):
    unit = materials.find_unit(course, TEST)
    assert unit.from_module and unit.name == "Unit 2: Vectors"
    materials.find_unit(course, TEST)
    assert canvas_api.count(f"/api/v1/courses/{COURSE}/modules") == 1


def test_quiz_items_match_by_title(course, canvas_api):
    MODULES[1]["items"][-1] = {"id": 25, "type": "Quiz", "title": TEST.name, "content_id": 900}
    try:
        assert materials.find_unit(course, TEST).name == "Unit 2: Vectors"
    finally:
        MODULES[1]["items"][-1] = {"id": 25, "type": "Assignment", "title": TEST.name, "content_id": 12}


def test_without_modules_the_unit_is_assignments_since_the_last_test(conn):
    old_test = Assignment(id=1, course_id=COURSE, name="Test 1: Kinematics", kind="test", due_at=at(2, 8))
    before = Assignment(id=2, course_id=COURSE, name="Free Fall HW", due_at=at(1, 23))
    after_test = Assignment(id=3, course_id=COURSE, name="Relative Velocity Lab", due_at=at(5, 23))
    undated = Assignment(id=4, course_id=COURSE, name="Practice: Unit Vectors")
    unrelated_undated = Assignment(id=5, course_id=COURSE, name="Course Expectations")
    later = Assignment(id=6, course_id=COURSE, name="Vectors Lab 2", due_at=at(20, 23))
    for a in (TEST, old_test, before, after_test, undated, unrelated_undated, later):
        db.save_assignment(conn, a)
    unit = materials.find_unit(conn, TEST)  # no Canvas token: no modules
    assert not unit.from_module
    assert {i["content_id"] for i in unit.items} == {3, 4}


# ---- gathering --------------------------------------------------------------------------------


def test_gather_reads_files_pages_docs_and_feedback_once(course, canvas_api, google):
    unit = materials.find_unit(course, TEST)
    docs = {m.title: m for m in materials.gather(course, TEST, unit)}
    assert set(docs) == {"Presentation: Vectors", "KEY: Unit Vectors", "Vectors HW", "Feedback on Vectors HW", "Extra Practice"}
    assert "tip to tail" in docs["Presentation: Vectors"].pages[0]
    assert docs["KEY: Unit Vectors"].pages[0].startswith("1. Find the unit vector")  # BOM stripped
    assert docs["Vectors HW"].kind == "google" and docs["Vectors HW"].url == HW.html_url
    assert "angle" in docs["Feedback on Vectors HW"].pages[0]
    assert not docs["Extra Practice"].readable and docs["Extra Practice"].note == "shared only inside the school"

    google.clear()
    canvas_api.clear()
    again = materials.gather(course, TEST, unit)
    assert len(again) == 5
    assert all("AAAA" not in u and "BBBB" not in u for u in google)  # read docs come from the cache
    assert not any("/files/" in p or "/pages/key-unit-vectors" in p for p in canvas_api)


def test_gather_without_canvas_uses_the_database_only(course, google):
    unit = materials.find_unit(course, TEST)
    titles = [m.title for m in materials.gather(course, TEST, unit)]
    assert "Vectors HW" in titles and TEST.name not in titles


# ---- excerpts ---------------------------------------------------------------------------------


def _doc(title: str, *pages: str) -> Material:
    return Material(key=title, title=title, kind="file", url=f"https://x/{title}", pages=list(pages))


def test_excerpts_cover_every_document_before_seconds_and_respect_the_budget():
    big = _doc("Slides", "cooking recipes " * 50, "vectors and components " * 50, "relative motion of boats " * 50)
    small = _doc("Review", "1. Add the vectors <1,2> and <3,4>.")
    picked = materials.excerpts([big, small], "Test 2: Vectors & Relative Motion", budget=2_000)
    assert sum(len(e.text) for e in picked) <= 2_000
    assert {e.title for e in picked} == {"Slides", "Review"}
    assert [e.id for e in picked] == [f"S{n}" for n in range(1, len(picked) + 1)]
    slides = [e for e in picked if e.title == "Slides"]
    assert slides[0].page in (2, 3) and "cooking" not in slides[0].text  # best match first
    assert picked[-1].label == "Review"  # one-page documents have no page number


def test_excerpts_fill_with_review_sheets_before_labs():
    lab = _doc("Lab: Force Table", *(f"lab page {i} " + "v" * 300 for i in range(5)))
    review = _doc("Test Review: Vectors", *(f"review page {i} " + "v" * 300 for i in range(5)))
    picked = materials.excerpts([lab, review], "vectors", budget=2_000)
    titles = [e.title for e in picked]
    assert titles.count("Lab: Force Table") == 1 and titles.count("Test Review: Vectors") == 5


def test_chunks_split_long_pages():
    doc = _doc("Long", "\n\n".join(f"Paragraph {i} " + "x" * 400 for i in range(10)))
    chunks = materials._chunks(doc)
    assert len(chunks) > 1 and all(len(t) <= materials.CHUNK_CHARS for _, t in chunks)


# ---- practice sets ----------------------------------------------------------------------------


def q(prompt: str, difficulty: int, source: str) -> practice.QuestionDraft:
    return practice.QuestionDraft(topic="vectors", qtype="calculation", prompt=prompt, choices=[], answer="5 m/s",
                                  explanation="", difficulty=difficulty, source=source)


CARD = PrepCard(assignment_id=12, kind="test", summary="", first_step="x", steps=[])


def test_practice_cites_unit_documents_easiest_first(course, canvas_api, google, model):
    replies, prompts = model
    replies.append(practice.PracticeDraft(
        focus="Vector addition and river crossings.",
        first_step="Read practice question 1 below and write the formula for a vector's magnitude.",
        questions=[q("hard", 3, "S3"), q("warm", 1, "[S2]"), q("mid", 2, "S1"), q("made up", 2, "S99")],
    ))
    replies.append(practice.Solved(answers=["5 m/s"] * 3))
    card = practice.add_practice(course, TEST, CARD)

    prompt = prompts[0][0]
    assert "Unit: Unit 2: Vectors" in prompt and "[S1] Presentation: Vectors" in prompt
    assert "tip to tail" in prompt and "Include the angle" in prompt
    assert "Extra Practice (not read: shared only inside the school)" in prompt

    assert [p.prompt for p in card.practice] == ["warm", "mid", "hard"]  # uncited one dropped
    assert card.practice[0].source == "KEY: Unit Vectors" and card.practice[0].source_url == "https://canvas.test/items/22"
    assert card.first_step.startswith("Read practice question 1 below and write the formula")
    assert card.focus.endswith("Built from Unit 2: Vectors.")
    sources = {s.name: s for s in card.sources}
    assert {"Presentation: Vectors", "KEY: Unit Vectors", "Vectors HW", "Extra Practice"} <= set(sources)
    assert not sources["Extra Practice"].read and sources["Extra Practice"].reason
    assert "Feedback on Vectors HW" not in sources


def test_first_step_falls_back_when_the_warmup_was_dropped(course, canvas_api, google, model):
    replies, _ = model
    replies.append(practice.PracticeDraft(
        focus="f", first_step="Read practice question 1 below and write the warm-up formula.",
        questions=[q("warm", 1, "S1"), q("a", 2, "S1"), q("b", 2, "S2"), q("c", 3, "S3")],
    ))
    replies.append(practice.Solved(answers=["wrong", "5 m/s", "5 m/s", "5 m/s"]))
    replies.append(practice.Judged(same=[False]))
    card = practice.add_practice(course, TEST, CARD)
    assert [p.prompt for p in card.practice] == ["a", "b", "c"]
    assert card.first_step == "Read practice question 1 below (vectors) and write down just the formula or rule you'd use."


def test_practice_without_materials_still_works(conn, model):
    replies, prompts = model
    db.save_assignment(conn, TEST)
    replies.append(practice.PracticeDraft(focus="f", questions=[q("a", 1, ""), q("b", 2, "")]))
    replies.append(practice.Solved(answers=["5 m/s", "5 m/s"]))
    card = practice.add_practice(conn, TEST, CARD)
    assert "(no readable documents" in prompts[0][0]
    assert [p.prompt for p in card.practice] == ["a", "b"] and card.practice[0].source == ""
    assert "Built from" not in card.focus
