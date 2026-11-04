"""Glimmer in the app: the decider client, prep cards and graded tests that take its decisions, and the
stats baseline. The decider is a fake answering over httpx.MockTransport; nothing here starts a
real one, and the model is stubbed the way test_prep.py and test_grader.py stub it."""

import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, decider_client, grader, meta_client, practice, prep
from app.clock import clock
from app.config import settings
from app.main import app
from app.models import Assignment, Block, Course, Link, Ping, RubricScore, TestRow
from tests.conftest import at
from tests.test_grader import FRENCH, blank_pdf, fake_model, quiz
from tests.test_prep import draft

LAB = Assignment(
    id=1, course_id=1, name="Lab 4", kind="lab", due_at=at(4, 23, 59), points_possible=20,
    submission_types=["online_upload"],
    description_html='<p>Do the lab. <a data-api-returntype="File" href="/files/9">Lab 4 handout.pdf</a></p>',
    rubric=[RubricScore(assignment_id=1, criterion="Data table", points_possible=10)],
    html_url="https://canvas.test/courses/1/assignments/1",
)


def decision(value, tier="instant", **kw):
    return {"value": value, "option_index": 0, "confidence": 0.99, "calibrated_confidence": 0.97, "letter_share": 1.0,
            "flipped": False, "tier": tier, "think_tokens": 0, "probe_tokens": 1, "trace": [],
            "prompt_version": "10", "model": "fake", "clarify": False, **kw}


class FakeDecider:
    """Answers /decide/assignment with `assignment` and /decide/test with test(questions) -> one
    {field: decision} per question; records every request. `status` fails every call."""

    def __init__(self, monkeypatch, assignment=None, test=None, status=200):
        self.requests: list[tuple[str, dict]] = []
        self.assignment = assignment or {"type": decision("reading"), "template": decision("document"),
                                         "effort": decision("one_short_sitting")}
        self.test = test
        self.status = status
        object.__setattr__(settings, "decider_url", "http://decider.test")
        monkeypatch.setattr(decider_client, "_client", lambda timeout: httpx.Client(
            base_url="http://decider.test", transport=httpx.MockTransport(self.handle)))

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append((request.url.path, body))
        if self.status != 200:
            return httpx.Response(self.status, text="llama-server: down")
        if request.url.path == "/decide/assignment":
            return httpx.Response(200, json={"decisions": self.assignment, "timings": {}})
        questions = body["questions"]
        decided = self.test(questions)
        return httpx.Response(200, json={
            "decisions": {q["id"]: d for q, d in zip(questions, decided)}, "new_topics": [], "timings": {}})


@pytest.fixture
def spark(monkeypatch):
    """Spark as a queue of canned card drafts."""
    object.__setattr__(settings, "meta_api_key", "key")
    replies, prompts = [], []

    def fake(prompt, schema, **kw):
        prompts.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr(meta_client, "generate_structured", fake)
    return replies, prompts


# ---- the client -------------------------------------------------------------------------------


def test_assignment_payload_is_what_the_labeling_page_shows(conn):
    p = decider_client.assignment_payload(LAB, "Chemistry")
    assert p == {
        "course": "Chemistry", "title": "Lab 4", "due": "Wed Nov 4, 2026, 11:59 PM", "points": 20,
        "submission_types": ["online_upload"], "attachments": ["Lab 4 handout.pdf"],
        "rubric": [{"criterion": "Data table", "points": 10}], "description": "Do the lab. Lab 4 handout.pdf",
    }


@pytest.mark.parametrize("type_, template, effort, kind, template_out, quick, level", [
    ("problem_set_or_worksheet", "checklist", "multi_day", "problem_set", "checklist", False, "heavy"),
    ("video_or_audio", "cornell", "one_solid_session", "other", "cornell", False, "medium"),
    ("something_else", "quick", "one_short_sitting", "other", "", True, "light"),
    ("test_or_quiz", "dashboard", "two_or_three_sessions", "test", "dashboard", False, "heavy"),
    ("lab_report", "dashboard", "one_solid_session", "lab", "dashboard", False, "medium"),
    ("essay_or_written_response", "checklist", "one_short_sitting", "essay", "checklist", False, "light"),
])
def test_glimmers_answers_become_the_apps_kind_effort_and_template(monkeypatch, type_, template, effort, kind,
                                                                  template_out, quick, level):
    FakeDecider(monkeypatch, {"type": decision(type_), "template": decision(template, "default"),
                              "effort": decision(effort, "guessed")})
    got = decider_client.decide_assignment(LAB, "Chemistry")
    assert (got.kind, got.template, got.quick, got.effort) == (kind, template_out, quick, level)
    assert got.records["effort"] == {"value": effort, "tier": "guessed", "confidence": 0.97, "clarify": False}


@pytest.mark.parametrize("how", ["status", "refused", "junk", "missing field"])
def test_a_decider_that_cant_answer_returns_none(monkeypatch, how):
    fake = FakeDecider(monkeypatch)
    if how == "status":
        fake.status = 503
    elif how == "refused":
        def down(request):
            raise httpx.ConnectError("refused")
        monkeypatch.setattr(decider_client, "_client", lambda timeout: httpx.Client(
            base_url="http://decider.test", transport=httpx.MockTransport(down)))
    elif how == "junk":
        monkeypatch.setattr(decider_client, "_client", lambda timeout: httpx.Client(
            base_url="http://decider.test", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))))
    else:
        fake.assignment = {"type": decision("reading")}
        fake.test = lambda qs: []  # answers for none of the questions
    assert decider_client.decide_assignment(LAB, "Chemistry") is None
    assert decider_client.decide_test(1, "French", [], [{"text": "q"}]) is None


def test_the_decider_is_never_called_when_its_url_is_empty(monkeypatch):
    fake = FakeDecider(monkeypatch)
    object.__setattr__(settings, "decider_url", "")
    assert decider_client.decide_assignment(LAB, "Chemistry") is None
    assert decider_client.decide_test(1, "French", [], [{"text": "q"}]) is None
    assert not decider_client.healthy() and fake.requests == []


def test_a_test_goes_with_thinking_off_and_at_most_25_topics(monkeypatch):
    fake = FakeDecider(monkeypatch, test=lambda qs: [{"format": decision("calculation")} for _ in qs])
    topics = [f"t{i}" for i in range(30)]
    out = decider_client.decide_test(7, "Calculus", topics, [{"text": "a"}, {"text": "b"}])
    path, body = fake.requests[0]
    assert path == "/decide/test" and body["think"] is False and body["class_id"] == 7
    assert body["topics"] == topics[:25] and [q["id"] for q in body["questions"]] == ["q0", "q1"]
    assert [d["format"]["value"] for d in out] == ["calculation", "calculation"]


# ---- assignments ------------------------------------------------------------------------------


def test_prep_card_takes_kind_effort_and_template_from_glimmer(conn, spark, monkeypatch):
    replies, prompts = spark
    db.save_course(conn, Course(id=1, name="Chemistry"))
    fake = FakeDecider(monkeypatch)
    replies.append(draft(kind="lab", effort="heavy"))  # Spark's own opinion, which loses
    card = prep.build_prep_card(conn, LAB)
    assert (card.kind, card.effort, card.template, card.quick) == ("reading", "light", "document", False)
    assert card.decisions["type"]["value"] == "reading" and card.decisions["template"]["tier"] == "instant"
    assert "Decided for you: kind reading; quick false; effort light" in prompts[0]
    assert "Kind guessed from the title" not in prompts[0]
    assert fake.requests[0][1]["assignment"]["course"] == "Chemistry"
    assert db.get_card(conn, 1) == card


def test_glimmer_can_make_it_a_quick_task_and_blanks_the_template(conn, spark, monkeypatch):
    replies, _ = spark
    FakeDecider(monkeypatch, {"type": decision("something_else"), "template": decision("quick"),
                              "effort": decision("one_short_sitting")})
    replies.append(draft(kind="other", steps=["a", "b"], effort="heavy", quick=False))
    card = prep.build_prep_card(conn, LAB)
    assert card.quick and card.steps == [] and card.effort == "light" and card.template == ""


def test_the_template_card_without_spark_still_takes_glimmers_calls(conn, monkeypatch):
    FakeDecider(monkeypatch, {"type": decision("something_else"), "template": decision("quick"),
                              "effort": decision("one_short_sitting")})
    card = prep.build_prep_card(conn, LAB)
    assert card.generated_by == "fallback" and card.quick and card.steps == [] and card.effort == "light"
    assert card.decisions["template"]["value"] == "quick"

    FakeDecider(monkeypatch, {"type": decision("problem_set_or_worksheet"), "template": decision("checklist"),
                              "effort": decision("two_or_three_sessions")})
    card = prep.build_prep_card(conn, LAB)
    assert (card.kind, card.effort, card.template, card.quick) == ("problem_set", "heavy", "checklist", False)
    assert card.steps  # the problem-set template's steps, not the lab's


def test_a_decider_that_is_down_leaves_spark_in_charge(conn, spark, monkeypatch):
    replies, prompts = spark
    FakeDecider(monkeypatch, status=503)
    replies.append(draft(kind="lab", effort="heavy"))
    card = prep.build_prep_card(conn, LAB)
    assert (card.kind, card.effort, card.template, card.decisions) == ("lab", "heavy", "", {})
    assert "Kind guessed from the title" in prompts[0]


def test_a_canvas_quiz_stays_a_test_whatever_glimmer_says(conn, spark, monkeypatch):
    replies, _ = spark
    monkeypatch.setattr(practice, "add_practice", lambda conn, a, card: card)  # a test card's practice set is another call
    FakeDecider(monkeypatch)  # reading
    replies.append(draft(kind="test"))
    quiz_ = LAB.model_copy(update={"submission_types": ["online_quiz"]})
    assert prep.build_prep_card(conn, quiz_).kind == "test"


def test_a_sync_decides_through_the_decider_one_card_at_a_time(conn, spark, monkeypatch):
    replies, _ = spark
    fake = FakeDecider(monkeypatch)
    for i in (1, 2, 3):
        db.save_assignment(conn, LAB.model_copy(update={"id": i, "due_at": at(4, 12 + i)}))
        replies.append(draft())
    cards = prep.build_all_missing(conn, at(2, 9))
    assert len(cards) == 3 and all(c.decisions for c in cards)
    assert len([p for p, _ in fake.requests if p == "/decide/assignment"]) == 3


# ---- graded tests -----------------------------------------------------------------------------


def labels(fmt="fill_in_the_blank", topic="passe_compose", credit="full", tier="instant"):
    return {"format": decision(fmt), "topic": decision(topic, tier), "credit": decision(credit)}


def french_rows(conn):
    db.save_course(conn, Course(id=FRENCH, name="French II"))
    test, rows, used = grader.reconcile(quiz(), FRENCH, FRENCH, "quiz.pdf", grader.seed_qtypes("French II"))
    return rows


def test_label_rows_sets_format_topic_and_credit_with_thinking_off(conn, monkeypatch):
    rows = french_rows(conn)
    seen = {}

    def answer(qs):
        seen["questions"] = qs
        return [labels("conjugation", "etre_vs_avoir", "none") if i == 0 else labels() for i in range(len(qs))]

    fake = FakeDecider(monkeypatch, test=answer)
    out = grader.label_rows(conn, Course(id=FRENCH, name="French II"), quiz(), rows)
    path, body = fake.requests[0]
    assert body["think"] is False and body["course"] == "French II" and body["class_id"] == FRENCH
    assert len(out) == len(rows) == len(seen["questions"]) == 22
    assert body["topics"] == ["passe_compose"]  # the topics Spark gave the test's sections
    first = out[0]
    assert (first.qtype, first.topic, first.credit) == ("conjugation", "etre_vs_avoir", "none")
    assert first.decisions["format"]["tier"] == "instant"
    assert out[1].qtype == "fill_in_the_blank" and all(r.decisions for r in out)
    # the marks stay the grader's
    assert [(r.marks, r.marks_possible) for r in out] == [(r.marks, r.marks_possible) for r in rows]


def test_the_question_rows_glimmer_reads_have_the_marks_as_written(conn):
    qs = grader.page_rows(quiz())
    assert len(qs) == 22 and qs[0]["marks"] == "" and qs[1]["marks"] == "X"
    assert qs[10]["marks"] == "-1" and qs[10]["note"] == "être, pas avoir" and qs[11]["marks"] == "0.5"
    assert qs[0]["section"] == "Verb from word bank instructions."


def test_credit_is_recorded_beside_the_marks_never_over_them(conn, monkeypatch):
    rows = french_rows(conn)
    wrong = next(i for i, r in enumerate(rows) if r.correct is False)
    FakeDecider(monkeypatch, test=lambda qs: [labels(credit="full") for _ in qs])
    out = grader.label_rows(conn, Course(id=FRENCH, name="French II"), quiz(), rows)
    assert out[wrong].credit == "full" and out[wrong].correct is False  # the teacher's X stands
    assert [r.correct for r in out] == [r.correct for r in rows]


def test_an_unnamed_topic_keeps_the_one_spark_gave(conn, monkeypatch):
    rows = french_rows(conn)
    FakeDecider(monkeypatch, test=lambda qs: [labels(topic="none_fit", tier="guessed") for _ in qs])
    out = grader.label_rows(conn, Course(id=FRENCH, name="French II"), quiz(), rows)
    assert [r.topic for r in out] == [r.topic for r in rows] and out[0].decisions["topic"]["tier"] == "guessed"


def test_rows_stay_as_spark_made_them_when_glimmer_cant_label(conn, monkeypatch):
    rows = french_rows(conn)
    fake = FakeDecider(monkeypatch, status=503)
    assert grader.label_rows(conn, Course(id=FRENCH, name="French II"), quiz(), rows) == rows
    assert grader.label_rows(conn, None, quiz(), rows) == rows  # no course, no class to label by
    assert grader.label_rows(conn, Course(id=FRENCH, name="French II"), quiz()[:1], rows) == rows  # counts differ
    assert len(fake.requests) == 1  # only the first got as far as asking


def test_labels_survive_the_database(conn):
    db.save_test_rows(conn, [TestRow(course_id=1, source="q.pdf", question="Q", qtype="conjugation", credit="partial",
                                     decisions={"format": {"value": "conjugation", "tier": "instant"}})])
    (row,) = db.list_test_rows(conn)
    assert row.credit == "partial" and row.decisions["format"]["tier"] == "instant"
    db.save_test_rows(conn, [TestRow(course_id=1, source="q.pdf", question="no labels")])
    assert db.list_test_rows(conn)[1].decisions == {} and db.list_test_rows(conn)[1].credit == ""


def test_an_upload_is_labeled_by_glimmer_and_feeds_the_grid(conn, monkeypatch):
    db.save_course(conn, Course(id=FRENCH, name="French II"))
    object.__setattr__(settings, "meta_api_key", "test-key")
    monkeypatch.setattr(grader, "read_pages", lambda conn, path, course_id=None: quiz())
    fake = FakeDecider(monkeypatch, test=lambda qs: [labels("conjugation", "etre_vs_avoir") for _ in qs])
    with TestClient(app) as client:
        r = client.post("/api/tests/upload", files={"file": ("quiz.pdf", b"%PDF-1.4", "application/pdf")})
        assert r.status_code == 200 and r.json()["saved"] == 22
        assert {x["qtype"] for x in r.json()["rows"]} == {"conjugation"}
        grid = client.get(f"/api/dashboard?course_id={FRENCH}").json()["grid"]
        assert grid["qtypes"] == ["conjugation"] and grid["topics"] == ["etre_vs_avoir"]
    assert [p for p, _ in fake.requests] == ["/decide/test"]


def test_relabel_labels_saved_tests_from_their_cached_pages(conn, tmp_path, monkeypatch):
    db.save_course(conn, Course(id=FRENCH, name="French II"))
    pdf = tmp_path / "files" / "tests" / "quiz.pdf"
    pdf.parent.mkdir(parents=True)
    blank_pdf(pdf, 2)
    prompts = fake_model(monkeypatch, quiz()[:2])
    test, rows, used = grader.read_test(conn, pdf)  # saved before Glimmer was on: Spark's types
    saved = grader.save(conn, test, rows, used)
    assert prompts and {r.qtype for r in db.list_test_rows(conn)} >= {"word_bank"}

    prompts.clear()
    fake = FakeDecider(monkeypatch, test=lambda qs: [labels("conjugation") for _ in qs])
    report = grader.relabel_saved(conn)
    assert prompts == []  # the cached pages: no Spark calls
    assert len(report) == 1 and "labeled by Glimmer" in report[0]
    assert {r.qtype for r in db.list_test_rows(conn)} == {"conjugation"}
    (after,) = db.list_graded_tests(conn)
    assert after.course_id == FRENCH and after.created_at == saved.created_at and fake.requests

    fake.status = 503
    assert "not labeled" in grader.relabel_saved(conn)[0]
    assert {r.qtype for r in db.list_test_rows(conn)} == {"conjugation"}  # unchanged when it can't answer
    fake.status = 200
    pdf.unlink()  # the photo is gone but its reading is cached by hash: still labeled
    assert "labeled by Glimmer" in grader.relabel_saved(conn)[0]
    for reading in (tmp_path / "files" / "tests" / "readings").glob("*.json"):
        reading.unlink()
    assert "skipped" in grader.relabel_saved(conn)[0]


# ---- stats ------------------------------------------------------------------------------------


@pytest.fixture
def started(conn):
    """Two blocks with pings: one missed start, and one start 5 minutes after its ping."""
    db.save_assignment(conn, LAB)
    old = db.save_block(conn, Block(assignment_id=1, start=at(2, 15), end=at(2, 16), status="missed"))
    db.save_ping(conn, Ping(block_id=old, sent_at=at(2, 15)))
    ok = db.save_block(conn, Block(assignment_id=1, start=at(2, 17), end=at(2, 18), status="started"))
    db.save_ping(conn, Ping(block_id=ok, sent_at=at(2, 17), answer="start_now", started_at=at(2, 17, 5)))
    return old, ok


def test_stats_count_everything_until_they_are_restarted(conn, started):
    with TestClient(app) as client:
        s = client.get("/api/stats").json()
        assert (s["pings"], s["starts"], s["avg_minutes_to_start"], s["blocks"]["missed"], s["since"]) == (2, 1, 5.0, 1, None)

        clock.offset = at(3, 9) - clock.real_now()
        r = client.post("/api/stats/reset").json()
        assert (r["pings"], r["starts"], r["avg_minutes_to_start"], r["blocks"], r["answers"]) == (0, 0, None, {}, {})
        assert r["since"].startswith("2026-11-03T09:00") and all(d["missed"] == 0 for d in r["weekdays"])

        new = db.save_block(conn, Block(assignment_id=1, start=at(3, 15), end=at(3, 16), status="started"))
        db.save_ping(conn, Ping(block_id=new, sent_at=at(3, 15), answer="start_now", started_at=at(3, 15) + timedelta(minutes=2)))
        s = client.get("/api/stats").json()
        assert (s["pings"], s["starts"], s["avg_minutes_to_start"], s["blocks"]) == (1, 1, 2.0, {"started": 1})


def test_restarting_the_stats_deletes_nothing_the_check_in_needs(conn, started):
    before = (db.list_pings(conn), db.list_blocks(conn))
    with TestClient(app) as client:
        client.post("/api/stats/reset")
    assert (db.list_pings(conn), db.list_blocks(conn)) == before
