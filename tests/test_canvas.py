"""Canvas client against a fake Canvas (httpx MockTransport): pagination, mapping, feedback."""

import httpx
import pytest

from app import canvas
from app.config import settings
from app.models import Assignment

ASSIGNMENT_JSON = {
    "id": 11,
    "name": "Lab 4: Osmosis",
    "description": (
        '<p>Use the <a href="/courses/1/files/5?wrap=1" data-api-endpoint="https://canvas.test/api/v1/courses/1/files/5" '
        'data-api-returntype="File">handout</a> and the <a href="https://docs.google.com/d/abc">template</a>.</p>'
        "<ul><li>Hypothesis</li><li>Data</li></ul>"
    ),
    "due_at": "2026-11-04T06:59:00Z",
    "points_possible": 20,
    "html_url": "https://canvas.test/courses/1/assignments/11",
    "submission_types": ["online_upload"],
    "rubric": [{"id": "c1", "description": "Observations", "points": 5}],
    "submission": {"workflow_state": "graded", "score": 15, "graded_at": "2026-10-20T20:00:00Z"},
}


@pytest.fixture
def fake_canvas(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        assert request.headers["Authorization"] == "Bearer tok"
        if path == "/api/v1/courses" and request.url.params.get("page") != "2":
            return httpx.Response(
                200,
                json=[{"id": 1, "name": "Biology"}, {"id": 2, "access_restricted_by_date": True}],
                headers={"Link": '<https://canvas.test/api/v1/courses?page=2>; rel="next"'},
            )
        if path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": 3, "name": "Spanish"}])
        if path == "/api/v1/courses/1/assignments":
            return httpx.Response(200, json=[ASSIGNMENT_JSON])
        if path.endswith("/submissions/self"):
            return httpx.Response(
                200,
                json={
                    "score": 15,
                    "submission_comments": [{"id": 100, "author_name": "Ms. R", "comment": "More detail", "created_at": None}],
                    "rubric_assessment": {"c1": {"points": 2, "comments": "Thin"}},
                },
            )
        return httpx.Response(404)

    object.__setattr__(settings, "canvas_token", "tok")
    monkeypatch.setattr(
        canvas,
        "_client",
        lambda: httpx.Client(
            base_url="https://canvas.test",
            headers={"Authorization": "Bearer tok"},
            transport=httpx.MockTransport(handler),
        ),
    )
    return calls


def test_courses_follow_pagination_and_skip_restricted(fake_canvas):
    assert [c.name for c in canvas.fetch_courses()] == ["Biology", "Spanish"]


def test_assignment_mapping(fake_canvas):
    [a] = canvas.fetch_assignments(1)
    assert a.kind == "lab"
    assert a.submitted and a.score == 15
    assert a.attachment_urls == ["https://canvas.test/api/v1/courses/1/files/5"]
    assert a.rubric[0].criterion == "Observations" and a.rubric[0].points is None


def test_feedback_fills_comments_and_rubric_points(fake_canvas):
    [a] = canvas.fetch_assignments(1)
    a = canvas.fetch_feedback(1, a)
    assert a.comments[0].text == "More detail" and a.comments[0].author == "Ms. R"
    assert a.rubric[0].points == 2 and a.rubric[0].comment == "Thin"


def test_parse_description():
    text, links, files = canvas.parse_description(ASSIGNMENT_JSON["description"], "https://canvas.test")
    assert "Use the handout and the template" in text
    assert "- Hypothesis" in text
    assert [l.url for l in links] == [
        "https://canvas.test/courses/1/files/5?wrap=1",
        "https://docs.google.com/d/abc",
    ]
    assert files == ["https://canvas.test/api/v1/courses/1/files/5"]


@pytest.mark.parametrize(
    "name,types,kind",
    [
        ("Unit 3 Quiz", [], "test"),
        ("Chapter check", ["online_quiz"], "test"),
        ("Lab 2: Density", [], "lab"),
        ("Argument Essay Draft", [], "essay"),
        ("Reading Response 4", [], "reading"),
        ("Problem Set 6", [], "problem_set"),
        ("Field trip form", [], "other"),
    ],
)
def test_guess_kind(name, types, kind):
    assert canvas.guess_kind(Assignment(id=1, course_id=1, name=name, submission_types=types)) == kind
