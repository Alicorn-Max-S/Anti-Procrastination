"""Homework-card quality: the kind rule, watch-out filtering, reading plans and the notes setting."""

from dataclasses import dataclass

import pytest

from app import canvas, db, meta_client, prep, reading, student_model
from app.config import settings
from app.models import Assignment, Link, RubricScore, SubmissionComment
from tests.conftest import at

# ---- kind --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,kind",
    [
        ("AP Calc Exam Derivative Problems", "problem_set"),
        ("Test Review: Vectors", "problem_set"),
        ("Quiz 3 Corrections", "problem_set"),
        ("Test #2: Vectors & Relative Motion", "test"),
        ("Unit 4 Quiz", "test"),
    ],
)
def test_title_guess_keeps_homework_about_tests_out_of_test(name, kind):
    assert canvas.guess_kind(Assignment(id=1, course_id=1, name=name)) == kind


def draft(**kw):
    base = dict(kind="problem_set", summary="Finish the problems.", first_step="Open the problems.",
                steps=["a", "b", "c"], links=[], watch_out="", effort="medium")
    return prep.CardDraft(**{**base, **kw})


@pytest.fixture
def model(monkeypatch):
    object.__setattr__(settings, "meta_api_key", "key")
    replies, prompts = [], []

    def fake(prompt, schema, **kw):
        prompts.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr(meta_client, "generate_structured", fake)
    return replies, prompts


def test_model_decides_kind_and_only_tests_get_practice(conn, model):
    replies, prompts = model
    # The title says "Exam", the description says homework: the model's read wins, no practice set.
    calc = Assignment(id=1, course_id=1, name="AP Calc Exam Derivative Problems", kind="test",
                      description_html="<p>Finish the problems started in class and check them against the KEY.</p>")
    replies.append(draft(kind="problem_set"))
    card = prep.build_prep_card(conn, calc)
    assert card.kind == "problem_set" and card.practice == [] and card.first_step == "Open the problems."
    assert "guessed from the title only" in prompts[0]


def test_a_canvas_quiz_is_always_a_test():
    quiz = Assignment(id=1, course_id=1, name="Chapter check", submission_types=["online_quiz"])
    assert prep.decide_kind(quiz, "reading") == "test"
    assert prep.decide_kind(Assignment(id=2, course_id=1, name="x"), "lab") == "lab"


def test_missing_instructions_are_said_not_invented(conn, model):
    replies, prompts = model
    lab = Assignment(id=3, course_id=1, name="Lab: Vector Force Table", kind="lab",
                     description_html='<iframe title="Vector Force Table Lab" src="https://example.com/x"></iframe>')
    replies.append(draft(kind="lab"))
    card = prep.build_prep_card(conn, lab)
    assert "Instructions available: NO" in prompts[0] and not card.instructions_seen
    template = prep.fallback_card(conn, lab)
    assert not template.instructions_seen and "find the handout" in template.first_step


# ---- watch-outs --------------------------------------------------------------------------------


def test_actionable_keeps_criticism_and_drops_grades_praise_and_admin():
    assert student_model.actionable("OG: 78.6%") == ""
    assert student_model.actionable("92/100") == ""
    assert student_model.actionable(
        "Max, you can find the scored rubric and the feedback on the shared Google doc. "
        "Please schedule a conference with me if you'd like to discuss the feedback."
    ) == ""
    praise_then_tip = (
        "Very well done.\xa0 You have a clear response and good evidence supporting the claim. "
        "The only thing I might suggest would be a brief explanation of why it was necessary."
    )
    assert student_model.actionable(praise_then_tip) == (
        "The only thing I might suggest would be a brief explanation of why it was necessary."
    )


def _past(conn, id, kind, comment="", rubric=None):
    db.save_assignment(conn, Assignment(
        id=id, course_id=1, name=f"Past {kind} {id}", kind=kind, rubric=rubric or [],
        comments=[SubmissionComment(id=id, assignment_id=id, author="Ms. R", text=comment)] if comment else [],
    ))


def test_watch_out_needs_actionable_feedback_that_fits(conn):
    _past(conn, 10, "test", "OG: 78.6%")
    _past(conn, 11, "essay", "Very well done. Next time, cite the page for every quote.")
    _past(conn, 12, "lab", rubric=[RubricScore(assignment_id=12, criterion="Units", points=4.85, points_possible=5)])
    lab = Assignment(id=20, course_id=1, name="Lab 5", kind="lab")
    essay = Assignment(id=21, course_id=1, name="Essay 2", kind="essay")
    assert student_model.watch_out_line(conn, lab) == ""  # grade note, wrong-kind tip, tiny loss: nothing
    assert "cite the page" in student_model.watch_out_line(conn, essay)
    assert "Very well done" not in student_model.watch_out_line(conn, essay)
    shares = Assignment(id=22, course_id=1, name="Lab 6", kind="lab",
                        rubric=[RubricScore(assignment_id=22, criterion="Citations")])
    _past(conn, 13, "essay", rubric=[RubricScore(assignment_id=13, criterion="Citations", points=1, points_possible=4)])
    assert "Citations" in student_model.watch_out_line(conn, shares)  # shared rubric criterion fits


def test_model_watch_out_must_quote_fitting_feedback_or_the_instructions(conn, model):
    replies, _ = model
    _past(conn, 11, "essay", "Next time, cite the page for every quote.")
    reading_hw = Assignment(id=30, course_id=1, name="Chapter 7", kind="reading",
                            description_html="<p>Pace yourself and don't put it off until the night before.</p>")
    replies.append(draft(kind="reading", watch_out='Ms. R said: "cite the page for every quote."'))  # essay, not reading
    assert prep.build_prep_card(conn, reading_hw).watch_out == ""
    replies.append(draft(kind="reading", watch_out='Your teacher says to “pace yourself and don’t put it off”.'))
    assert "pace yourself" in prep.build_prep_card(conn, reading_hw).watch_out
    replies.append(draft(kind="essay", watch_out='Ms. R: "always cite three sources"'))  # invented quote
    assert prep.build_prep_card(conn, reading_hw).watch_out.startswith("Ms. R on Past essay 11")


# ---- reading plans -----------------------------------------------------------------------------


@dataclass
class Page:
    number: str | None
    text: str


def _prose(n: int, seed: str = "people") -> str:
    line = f"The network moved {seed} north through towns and courts across many long years"
    return "\n".join([line] * n)


def chapter() -> list[Page]:
    """A fabricated 12-page chapter: an unnumbered opener with front matter, then pages 2-12 with
    running heads and four section headings."""
    pages = [Page(None, "A HISTORY\nSome Author\nCHAPTER ONE\nThe Long Road\n" + _prose(20))]
    heads = {4: "The Northern Star: How Canada Became Freedom", 7: "The Invention of Vigilance", 10: "The Verdict of History"}
    for n in range(2, 13):
        top = f"{n} Chapter One" if n % 2 == 0 else f"The Long Road {n}"
        body = _prose(25) + (f"\n{heads[n]}\n" if n in heads else "\n") + _prose(25)
        pages.append(Page(str(n), f"{top}\n{body}"))
    return pages


def test_reading_plan_counts_real_pages_and_splits_at_sections():
    plan = reading.plan(chapter(), "chapter.pdf")
    assert (plan.first_page, plan.last_page, plan.pages) == ("1", "12", 12)  # opener numbered from its neighbours
    titles = [t for s in plan.sittings for t in s.sections]
    assert titles == [reading.OPENING, "The Northern Star: How Canada Became Freedom",
                      "The Invention of Vigilance", "The Verdict of History"]
    assert "A HISTORY" not in titles and "Some Author" not in titles and "Chapter One" not in " ".join(titles)
    assert sum(s.words for s in plan.sittings) == plan.words and plan.minutes == round(plan.words / reading.WPM)
    assert len(plan.sittings) == min(reading.MAX_SITTINGS, 4, -(-plan.minutes // reading.SITTING_MINUTES))
    first = plan.sittings[0]
    assert reading.step(first).startswith(f"Read pp. 1–{first.last_page}: the opening")
    for a, b in zip(plan.sittings, plan.sittings[1:]):  # sittings follow each other, sharing at most a page
        assert int(b.first_page) - int(a.last_page) in (0, 1)


def test_reading_plan_respects_an_assigned_page_range():
    rng = reading.page_range("Read pp. 4-9 for Monday.")
    assert rng == (4, 9)
    plan = reading.plan(chapter(), "chapter.pdf", rng)
    assert (plan.first_page, plan.last_page, plan.pages) == ("4", "9", 6)
    assert plan.sittings[0].sections[0] == reading.OPENING  # starts mid-section on p. 4's first lines


def test_short_files_get_no_plan():
    assert reading.plan([Page("1", _prose(10))], "poem.pdf") is None


def test_reading_card_uses_the_plan_for_steps_and_effort(conn, model, monkeypatch):
    replies, prompts = model
    plan = reading.plan(chapter(), "chapter.pdf")
    monkeypatch.setattr(prep, "_attachments", lambda a: ([], []))
    monkeypatch.setattr(prep, "_reading_plan", lambda docs, text: plan)
    notes = [f"Note {i}" for i in range(len(plan.sittings))]
    replies.append(draft(kind="reading", steps=["Write one question for class."], reading_notes=notes))
    card = prep.build_prep_card(conn, Assignment(id=40, course_id=1, name="Chapter 1", description_html="<p>Read it.</p>"))
    assert card.reading == plan and len(card.steps) == len(plan.sittings) + 1
    assert card.steps[0].startswith("Read pp. 1–") and card.steps[0].endswith("Note 0")
    assert card.steps[-1] == "Write one question for class."
    assert card.effort == ("heavy" if len(plan.sittings) > 1 else card.effort)
    assert "Sitting 1: pp. 1–" in prompts[0]


# ---- notes medium ------------------------------------------------------------------------------


def test_notes_medium_is_per_course_and_neutral_by_default(conn, model):
    replies, prompts = model
    a = Assignment(id=50, course_id=7, name="Ch. 3", kind="reading", description_html="<p>Read.</p>")
    assert prep.notes_medium(conn, 7) is None
    assert "Notes for this course: not set" in prep._prompt(a, "History", "Read.", [], [])
    prep.set_notes_medium(conn, 7, "paper")
    replies.append(draft(kind="reading"))
    prep.build_prep_card(conn, a)
    assert "Notes for this course: on paper" in prompts[0]
    assert "notebook" in prep.fallback_card(conn, a.model_copy(update={"kind": "problem_set"})).first_step
    assert prep.notes_medium(conn, 8) is None
    with pytest.raises(ValueError):
        prep.set_notes_medium(conn, 7, "tablet")
    prep.set_notes_medium(conn, 7, None)
    assert prep.notes_medium(conn, 7) is None


def test_prompt_names_files_by_title(conn):
    a = Assignment(id=60, course_id=1, name="Lab", kind="lab", description_html="<p>x</p>")
    p = prep._prompt(a, "Physics", "x", [Link(label="l", url="https://e.x")], [], read_names=["Relative Velocity Inquiry Lab"])
    assert "Relative Velocity Inquiry Lab" in p and at(1, 1)  # at() keeps the conftest import honest


# ---- undo "Mark done" --------------------------------------------------------------------------


def test_undo_done_restores_each_block_sensibly():
    from app import scheduler
    from app.models import Block

    now = at(3, 12)
    blocks = [
        Block(id=1, assignment_id=9, start=at(2, 16), end=at(2, 17), status="done"),  # started yesterday
        Block(id=2, assignment_id=9, start=at(2, 18), end=at(2, 19), status="done"),  # never started
        Block(id=3, assignment_id=9, start=at(4, 16), end=at(4, 17), status="done"),  # still ahead
        Block(id=4, assignment_id=9, start=now, end=now, status="done"),  # Mark done's marker
        Block(id=5, assignment_id=8, start=at(4, 16), end=at(4, 17), status="done"),  # another assignment
    ]
    changed, deleted = scheduler.undo_done(blocks, 9, now, started={1})
    assert {b.id: b.status for b in changed} == {1: "started", 2: "missed", 3: "planned"}
    assert deleted == [4]


def test_undo_done_through_the_api(conn):
    from fastapi.testclient import TestClient

    from app import demo
    from app.main import app

    demo.seed(conn, now=at(2, 10))
    with TestClient(app) as client:
        client.post("/api/clock", json={"action": "set", "when": at(2, 10).isoformat()})
        card = lambda: next(c for c in client.get("/api/cards").json() if c["assignment_id"] == 9000204)
        planned = card()["next_block"]
        assert client.post("/api/assignments/9000204/undone").status_code == 409  # not done yet
        assert client.post("/api/assignments/9000204/done").status_code == 200
        assert card()["done"] and card()["next_block"] is None
        r = client.post("/api/assignments/9000204/undone")
        assert r.status_code == 200
        assert not card()["done"] and card()["next_block"] == planned  # its future block is back

        r = client.put(f"/api/courses/{card()['course_id']}/notes", json={"medium": "paper"})
        assert r.status_code == 200 and card()["notes_medium"] == "paper"
        assert client.put(f"/api/courses/{card()['course_id']}/notes", json={"medium": "slate"}).status_code == 422
