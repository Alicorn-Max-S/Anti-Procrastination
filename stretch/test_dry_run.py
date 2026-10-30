"""Dry run against a fake streaming Responses API: what the model is (and isn't) shown, the tool log,
reasoning replay, trap checks, the budget stop, and ranking by the student's history.
Run with `pytest stretch`; the main `pytest` run leaves stretch alone."""

import json

import httpx
import pytest

from app import db
from app.config import settings
from app.meta_client import MetaError
from app.models import Assignment, Course, PrepCard, RubricScore, SubmissionComment
from stretch import dry_run, evaluate
from tests.conftest import at

LAB = Assignment(
    id=1, course_id=1, name="Lab 4: Reaction Rates", kind="lab", due_at=at(4, 23, 59), points_possible=20,
    description_html="<p>Record the temperature every 30 seconds for each trial.</p>",
    html_url="https://canvas.test/courses/1/assignments/1",
    rubric=[RubricScore(assignment_id=1, criterion="Data table", points_possible=5)],
)

# A graded lab in the same course: its points and comments must never reach the model.
LAB3 = Assignment(
    id=3, course_id=1, name="Lab 3", kind="lab", submitted=True, score=15, graded_at=at(1, 16),
    rubric=[
        RubricScore(assignment_id=3, criterion="Data table", points=2, points_possible=5, comment="Units missing in column 3."),
        RubricScore(assignment_id=3, criterion="Conclusion", points=6, points_possible=6),
    ],
    comments=[SubmissionComment(id=1, assignment_id=3, author="T", text="Observations were thin again.")],
)


def call(call_id, name, **args):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}


def report(**kw):
    base = dict(
        task="A lab report on reaction rate vs temperature.",
        phases=[{"name": "Collect data", "what_you_do": "Run three trials.", "produces": "A raw data table",
                 "needs": ["thermometer"], "hardest": True}],
        done_looks_like=["Data table with units in every column"],
        traps=[], effort="heavy", effort_reasons=["Three trials plus analysis"], blocked=["The trials need the lab"],
    )
    return {**base, **kw}


def trap(**kw):
    base = dict(kind="ambiguous", where="Data table", quote="", what_happens="Unclear whether to average trials.",
                first_step="Ask in class.", rubric_criterion="", log_refs=[])
    return {**base, **kw}


def response(output, input_tokens=1000, cached=0, out=500, reasoning=0):
    return {
        "id": "resp_x", "status": "completed", "output": output,
        "usage": {"input_tokens": input_tokens, "output_tokens": out,
                  "input_tokens_details": {"cached_tokens": cached}, "output_tokens_details": {"reasoning_tokens": reasoning}},
    }


def final(payload, **usage):
    return response([{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": json.dumps(payload)}]}], **usage)


def sse(resp):
    events = [{"type": "response.created", "response": {"id": "resp_x", "status": "in_progress"}},
              {"type": "response.completed", "response": resp}]
    return ("".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n").encode()


@pytest.fixture
def fake_api(monkeypatch):
    """Queue of final responses to stream back, and every (path, body) sent."""
    queue, sent = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append((request.url.path, body))
        if request.url.path.endswith("/responses/input_tokens"):
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 1000})
        return httpx.Response(200, content=sse(queue.pop(0)), headers={"content-type": "text/event-stream"})

    object.__setattr__(settings, "meta_api_key", "key")
    monkeypatch.setattr(dry_run, "_client", lambda: httpx.Client(base_url="https://api.test/v1", transport=httpx.MockTransport(handler)))
    return queue, sent


@pytest.fixture
def course(conn):
    db.save_course(conn, Course(id=1, name="Chemistry"))
    db.save_assignment(conn, LAB)
    db.save_assignment(conn, LAB3)
    return conn


def test_materials_hold_the_assignment_but_no_grades_or_feedback(course):
    text = dry_run.materials(course, LAB3).text
    assert "Data table (5 pts)" in text and "Lab 3" in text
    for leaked in ("Units missing", "thin again", "15", "2 pts"):
        assert leaked not in text


def test_run_logs_tools_replays_reasoning_and_checks_traps(course, fake_api):
    queue, sent = fake_api
    queue.append(response(
        [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc1"},
         call("c1", "look_up", where="Part B", what="how to turn times into a rate"),
         call("c2", "log_trouble", kind="ambiguous", where="Data table", what_happened="average the trials or not?")],
        reasoning=400,
    ))
    queue.append(final(report(traps=[
        trap(quote="record the temperature   EVERY 30 seconds", rubric_criterion="Data table", log_refs=[2]),
        trap(kind="stuck", where="Part C", quote="words that aren't there", log_refs=[9]),
    ]), input_tokens=1600, cached=1000, out=800))

    result = dry_run.run(course, LAB)

    assert [p for p, _ in sent] == ["/v1/responses/input_tokens", "/v1/responses", "/v1/responses"]
    first = sent[1][1]
    assert first["model"] == "muse-wonder-1.0" and first["reasoning"] == {"effort": "high"}
    assert first["store"] is False and first["include"] == ["reasoning.encrypted_content"] and first["stream"] is True
    replayed = sent[2][1]["input"]
    assert replayed[1] == {"type": "reasoning", "summary": [], "encrypted_content": "enc1"}
    assert [i["type"] for i in replayed[2:]] == ["function_call", "function_call", "function_call_output", "function_call_output"]
    assert json.loads(replayed[4]["output"])["logged"] == 1

    assert [(e.n, e.kind, e.where) for e in result.log] == [(1, "lookup", "Part B"), (2, "ambiguous", "Data table")]
    good, bad = result.traps
    assert good.quote_found and good.observed
    assert not bad.quote_found and not bad.observed
    u = result.usage
    assert (u.turns, u.input_tokens, u.cached_tokens, u.output_tokens, u.reasoning_tokens) == (2, 2600, 1000, 1300, 400)
    assert u.cost_usd == pytest.approx((1600 * 4 + 1000 * 0.2 + 1300 * 20) / 1e6)
    assert dry_run.latest(course, LAB.id) == result


def test_budget_stops_before_another_turn(course, fake_api):
    queue, sent = fake_api
    queue.append(response([call("c1", "look_up", where="1", what="x")], input_tokens=100_000, out=30_000))
    with pytest.raises(MetaError, match="budget"):
        dry_run.run(course, LAB, budget=0.10)
    assert len([p for p, _ in sent if p == "/v1/responses"]) == 1
    assert dry_run.latest(course, LAB.id) is None


def test_contributor_models_are_refused(course, fake_api):
    with pytest.raises(MetaError, match="contributor"):
        dry_run.run(course, LAB, model="muse-spark-1.3-contributor")


def test_card_gets_the_trap_the_student_has_history_with(course, fake_api):
    queue, _ = fake_api
    queue.append(final(report(traps=[
        trap(kind="lookup", where="Part B", what_happens="Rate formula isn't given.", log_refs=[]),
        trap(where="Data table", rubric_criterion="Data table", first_step="Write units in every header first."),
    ])))
    result = dry_run.run(course, LAB)
    assert dry_run.ranked_traps(course, result)[0].where == "Data table"

    db.save_card(course, PrepCard(assignment_id=1, kind="lab", summary="", first_step="x", steps=[], watch_out="From feedback."))
    card = dry_run.apply_to_card(course, LAB.id)
    assert card.effort == "heavy" and card.watch_out.startswith("Data table:")


def test_evaluate_matches_lost_criteria(course, fake_api):
    queue, _ = fake_api
    queue.append(final(report(traps=[trap(rubric_criterion="data TABLE")])))
    result = dry_run.run(course, LAB3)
    c = evaluate.compare(LAB3, result)
    assert c["matched"] == ["Data table"] and c["missed"] == []
    assert c["comments"] == ["Observations were thin again."]
    assert [a.id for a in evaluate.candidates(course)] == [3]
