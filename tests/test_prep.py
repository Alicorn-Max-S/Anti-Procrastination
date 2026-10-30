"""Prep cards with the model stubbed out: link filtering, fallbacks, practice sets, the second solve."""

import pytest

from app import db, meta_client, practice, prep
from app.config import settings
from app.meta_client import MetaError
from app.models import Assignment, Link, PracticeQuestion, PrepCard, TestRow
from tests.conftest import at

LAB = Assignment(
    id=1, course_id=1, name="Lab 4", kind="lab", due_at=at(4, 23, 59),
    description_html='<p>Use the <a href="https://example.com/handout.pdf">handout</a>.</p>',
    html_url="https://canvas.test/courses/1/assignments/1",
)


def draft(**kw):
    base = dict(
        kind="lab", summary="A lab report.", first_step="Open the handout.", steps=["a", "b", "c"],
        links=[Link(label="handout", url="https://example.com/handout.pdf"), Link(label="made up", url="https://evil.test")],
        watch_out="", effort="heavy",
    )
    return prep.CardDraft(**{**base, **kw})


@pytest.fixture
def model(monkeypatch):
    """Replace the model with a queue of canned replies (or exceptions)."""
    object.__setattr__(settings, "meta_api_key", "key")
    replies, prompts = [], []

    def fake(prompt, schema, **kw):
        prompts.append((prompt, schema, kw))
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(meta_client, "generate_structured", fake)
    return replies, prompts


def test_fallback_card_without_a_key(conn):
    db.save_assignment(conn, LAB)
    card = prep.build_prep_card(conn, LAB)
    assert card.generated_by == "fallback" and "Lab 4" in card.first_step
    assert {l.url for l in card.links} == {"https://example.com/handout.pdf", LAB.html_url}
    assert db.get_card(conn, 1) == card


def test_model_card_drops_invented_links_and_offers_tools(conn, model):
    replies, prompts = model
    replies.append(draft())
    card = prep.build_prep_card(conn, LAB)
    assert card.generated_by == "model" and card.effort == "heavy"
    assert [l.url for l in card.links] == ["https://example.com/handout.pdf"]
    _, schema, kw = prompts[0]
    assert schema is prep.CardDraft
    assert {t.name for t in kw["tools"]} == {"past_feedback", "weak_spots"}


def test_model_failure_falls_back(conn, model):
    replies, _ = model
    replies.append(MetaError("boom"))
    assert prep.build_prep_card(conn, LAB).generated_by == "fallback"


def test_build_all_missing_only_upcoming_and_capped(conn, model):
    replies, _ = model
    now = at(2, 10)
    for i in range(1, 5):
        db.save_assignment(conn, LAB.model_copy(update={"id": i, "due_at": at(2 + i, 23)}))
    db.save_assignment(conn, LAB.model_copy(update={"id": 9, "due_at": at(1, 23)}))  # past
    db.save_assignment(conn, LAB.model_copy(update={"id": 8, "submitted": True}))
    replies.extend([draft(), draft()])
    built = prep.build_all_missing(conn, now, horizon_days=14, limit=2)
    assert [c.assignment_id for c in built] == [1, 2]


def test_second_solve_marks_and_judges(model):
    replies, _ = model
    qs = [
        PracticeQuestion(topic="t", qtype="mc", prompt="1?", choices=["a", "b"], answer="b) cantaba"),
        PracticeQuestion(topic="t", qtype="tr", prompt="2?", answer="Ella tuvo que salir."),
        PracticeQuestion(topic="t", qtype="tr", prompt="3?", answer="fuimos"),
    ]
    replies.append(practice.Solved(answers=["cantaba", "She had to leave: Ella tuvo que irse", "íbamos"]))
    replies.append(practice.Judged(same=[True, False]))
    out = practice.second_solve(qs)
    assert [q.verified for q in out] == [True, True, False]


def test_test_card_gets_practice_aimed_at_weak_spots(conn, model):
    replies, prompts = model
    quiz = Assignment(id=2, course_id=1, name="Quiz 4", kind="test", due_at=at(5, 9),
                      description_html="<p>Preterite vs imperfect.</p>")
    db.save_test_rows(conn, [TestRow(course_id=1, source="q.jpg", question="Q", student_answer="x", marks=0,
                                     marks_possible=2, topic="pret_imp", qtype="fill_in_blank")])
    q = practice.QuestionDraft(topic="pret_imp", qtype="fill_in_blank", prompt="P", choices=[], answer="era", explanation="")
    replies.append(draft(kind="test", links=[]))
    replies.append(practice.PracticeDraft(focus="Aiming at pret_imp.", questions=[q, q, q]))
    replies.append(practice.Solved(answers=["era", "era", "era"]))
    card = prep.build_prep_card(conn, quiz)
    assert len(card.practice) == 3 and all(p.verified for p in card.practice)
    assert card.first_step.startswith("Read practice question 1")
    assert "pret_imp / fill_in_blank" in prompts[1][0]


def test_practice_without_model_keeps_focus(conn):
    db.save_test_rows(conn, [TestRow(course_id=1, source="q.jpg", marks=0, marks_possible=2, topic="pret_imp", qtype="fill_in_blank")])
    card = PrepCard(assignment_id=2, kind="test", summary="", first_step="x", steps=[])
    out = practice.add_practice(conn, Assignment(id=2, course_id=1, name="Quiz"), card)
    assert out.practice == [] and "pret imp (fill in blank)" in out.focus
