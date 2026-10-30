"""Test prep: a short practice set aimed at your weakest topic x question-type cells.

Built from graded-test rows (grader.py) via the student model. Every answer is checked by a
second, independent solve; a third call judges answers that differ only in wording. Questions the
check can't confirm are dropped when enough confirmed ones remain, otherwise shown as unchecked.
"""

import logging
import re
import sqlite3

import httpx
from pydantic import BaseModel

from app import canvas, db, meta_client, student_model
from app.meta_client import MetaError
from app.models import Assignment, PracticeQuestion, PrepCard

log = logging.getLogger(__name__)

SET_SIZE = 6
MIN_VERIFIED = 3

INSTRUCTIONS = """You write short practice sets for a high-school student's upcoming quiz or test.
Aim most questions at the weak spots you're given (same topic AND same question type), modeled on
the questions the student actually missed, but never copy a past question. Each question must have
one clearly correct answer. For multiple choice, list the choices and give the correct choice's
full text as the answer. Keep explanations to one or two sentences."""


class QuestionDraft(BaseModel):
    topic: str
    qtype: str
    prompt: str
    choices: list[str]
    answer: str
    explanation: str


class PracticeDraft(BaseModel):
    focus: str  # one sentence: what this set targets and why
    questions: list[QuestionDraft]


class Solved(BaseModel):
    answers: list[str]


class Judged(BaseModel):
    same: list[bool]


def _norm(answer: str) -> str:
    answer = answer.strip().lower()
    answer = re.sub(r"^\(?[a-e][).:]\s+", "", answer)  # "b) cantaba" -> "cantaba"
    return re.sub(r"[^\w]+", " ", answer).strip()


def _focus_from_cells(cells: list[dict]) -> str:
    if not cells:
        return ""
    parts = [f"{c['topic'].replace('_', ' ')} ({c['qtype'].replace('_', ' ')})" for c in cells]
    return "From your graded tests, you lose the most marks on " + ", ".join(parts) + "."


def _listing(questions: list[PracticeQuestion]) -> str:
    lines = []
    for i, q in enumerate(questions, 1):
        lines.append(f"Q{i} [{q.qtype}] {q.prompt}")
        lines.extend(f"   - {c}" for c in q.choices)
    return "\n".join(lines)


def second_solve(questions: list[PracticeQuestion]) -> list[PracticeQuestion]:
    """Mark each question verified when an independent solve reaches the same answer."""
    solved = meta_client.generate_structured(
        "Solve each question independently. Return exactly one answer per question, in order. "
        "For multiple choice, give the full text of the choice.\n\n" + _listing(questions),
        Solved,
    )
    verified: set[int] = set()
    unclear: list[int] = []
    for i, q in enumerate(questions):
        if i >= len(solved.answers):
            continue
        if _norm(q.answer) == _norm(solved.answers[i]):
            verified.add(i)
        else:
            unclear.append(i)
    if unclear:
        pairs = "\n".join(
            f"{n}. Q: {questions[i].prompt}\n   A: {questions[i].answer}\n   B: {solved.answers[i]}"
            for n, i in enumerate(unclear, 1)
        )
        judged = meta_client.generate_structured(
            "For each numbered pair, is answer A correct and does B mean the same thing? "
            "Return one true/false per pair, in order.\n\n" + pairs,
            Judged,
        )
        verified.update(i for i, same in zip(unclear, judged.same) if same)
    return [q.model_copy(update={"verified": i in verified}) for i, q in enumerate(questions)]


def add_practice(conn: sqlite3.Connection, assignment: Assignment, card: PrepCard) -> PrepCard:
    """Return the card with a practice set, focus line and a practice-based first step."""
    cells = student_model.weakest_cells(conn, assignment.course_id, n=3)
    focus = _focus_from_cells(cells)
    if not meta_client.configured():
        return card.model_copy(update={"focus": focus})

    text, _, _ = canvas.parse_description(assignment.description_html)
    known_qtypes = sorted({r.qtype for r in db.list_test_rows(conn, assignment.course_id) if r.qtype})
    weak = "\n".join(
        f"- {c['topic']} / {c['qtype']}: lost {c['lost']:g} of {c['possible']:g} marks; missed e.g. "
        + "; ".join(f"{e['question']!r} (answered {e['student_answer']!r}, teacher: {e['teacher_note']!r})" for e in c["examples"])
        for c in cells
    )
    prompt = f"""Upcoming: {assignment.name}
What the teacher says it covers:
{text or "(no description)"}

Weak spots from this student's graded tests (worst first):
{weak or "(no graded tests yet: cover the main topics evenly)"}

Question types this class uses: {", ".join(known_qtypes) or "any"}

Write {SET_SIZE} practice questions."""
    try:
        draft = meta_client.generate_structured(prompt, PracticeDraft, instructions=INSTRUCTIONS)
        questions = [PracticeQuestion(**q.model_dump()) for q in draft.questions[:SET_SIZE]]
        try:
            questions = second_solve(questions)
            checked = [q for q in questions if q.verified]
            if len(checked) >= MIN_VERIFIED:
                questions = checked
        except (MetaError, httpx.HTTPError) as e:
            log.warning("second solve failed, showing unchecked questions: %s", e)
    except (MetaError, httpx.HTTPError) as e:
        log.warning("practice set for %s failed: %s", assignment.id, e)
        return card.model_copy(update={"focus": focus})

    first = questions[0] if questions else None
    update: dict = {"practice": questions, "focus": draft.focus or focus}
    if first:
        update["first_step"] = (
            f"Answer practice question 1 ({first.topic.replace('_', ' ')}, "
            f"{first.qtype.replace('_', ' ')}) without notes."
        )
    return card.model_copy(update=update)
