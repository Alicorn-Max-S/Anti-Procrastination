"""Test prep: a short practice set built from what the class covered, aimed at the weak spots.

The unit's documents come from materials.py (the Canvas module holding the test, or the
assignments due before it). Only the excerpts that best match the test's topics go to the model,
each with an id; every question must cite one, and the card shows that document. Weak spots come
from graded-test rows via the student model and from teacher feedback on earlier work.

Question 1 is the easiest, because the card's first step is starting it. Every answer is checked
by a second, independent solve; a third call judges answers that differ only in wording. Questions
the check can't confirm are dropped when enough confirmed ones remain, otherwise shown as unchecked.
"""

import logging
import re
import sqlite3

import httpx
from pydantic import BaseModel

from app import canvas, db, materials, meta_client, student_model
from app.materials import Excerpt, Material, Unit
from app.meta_client import MetaError
from app.models import Assignment, PracticeQuestion, PrepCard, SourceFile

log = logging.getLogger(__name__)

SET_SIZE = 6
MIN_VERIFIED = 3

INSTRUCTIONS = """You write short practice sets for a high-school student's upcoming quiz or test.
- Base every question on the class's own materials: the numbered excerpts you're given. Spread the
  questions over the topics they cover, and ask them the way this class does (look at how the review
  sheets, homework and tests word their questions: multiple choice, short calculation, multi-part
  word problem, ...).
- Write new questions in that style. Never copy a question from an excerpt; change the numbers and
  the situation.
- source: the id of the excerpt the question is based on, e.g. "S3". Only ids you were given.
- When weak spots from graded tests are listed, aim at least half the questions at them (same topic
  and same question type), modeled on what the student missed.
- difficulty: 1, 2 or 3. Question 1 is a warm-up with difficulty 1: one step, about two minutes.
  The others are 2 (a standard problem) or 3 (multi-step), easiest first.
- Each question has one clearly correct answer. For multiple choice, list the choices and give the
  correct choice's full text as the answer. Include units in numeric answers. Keep explanations to
  one or two sentences.
- first_step: one action of about two minutes that starts practice question 1 and names exactly
  what to write, e.g. "Read practice question 1 below and write the formula for a vector's
  x-component." Don't solve it for them.
- focus: one sentence on what the set covers and which weak spot it aims at, if any."""


class QuestionDraft(BaseModel):
    topic: str
    qtype: str
    prompt: str
    choices: list[str]
    answer: str
    explanation: str
    difficulty: int = 2
    source: str = ""


class PracticeDraft(BaseModel):
    focus: str  # one sentence: what this set targets and why
    questions: list[QuestionDraft]
    first_step: str = ""


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


# ---- the prompt -------------------------------------------------------------------------------


def _feedback_lines(conn: sqlite3.Connection, course_id: int, unit_feedback: list[Material]) -> str:
    """Teacher feedback on earlier work in this unit, plus on earlier tests anywhere in the course."""
    lines = [f"- {m.title}: " + " ".join(m.pages[0].split())[:400] for m in unit_feedback]
    for e in student_model.feedback_evidence(conn, course_id, kind="test", limit=5):
        if e["source"] == "comment":
            lines.append(f"- {e['assignment']}: teacher comment {e['text'][:300]!r}")
        elif e["source"] == "rubric":
            lines.append(f"- {e['assignment']}: lost {e['points_lost']:g} on {e['criterion']!r} {e['teacher_comment'][:200]!r}")
    return "\n".join(dict.fromkeys(lines))


def _prompt(
    conn: sqlite3.Connection,
    assignment: Assignment,
    unit: Unit,
    docs: list[Material],
    picked: list[Excerpt],
    cells: list[dict],
) -> str:
    courses = {c.id: c.name for c in db.list_courses(conn)}
    text, _, _ = canvas.parse_description(assignment.description_html)
    known_qtypes = sorted({r.qtype for r in db.list_test_rows(conn, assignment.course_id) if r.qtype})
    weak = "\n".join(
        f"- {c['topic']} / {c['qtype']}: lost {c['lost']:g} of {c['possible']:g} marks; missed e.g. "
        + "; ".join(f"{e['question']!r} (answered {e['student_answer']!r}, teacher: {e['teacher_note']!r})" for e in c["examples"])
        for c in cells
    )
    where = "its Canvas module" if unit.from_module else "assignments due before the test"
    listed = "\n".join(
        f"- {d.title}" + ("" if d.readable else f" (not read: {d.note or 'no text'})")
        for d in docs if d.kind != "feedback"
    )
    quoted = "\n\n".join(f"[{e.id}] {e.label}\n{e.text}" for e in picked)
    feedback = _feedback_lines(conn, assignment.course_id, [d for d in docs if d.kind == "feedback"])
    return f"""Upcoming: {assignment.name}
Course: {courses.get(assignment.course_id, "")}
What the teacher says it covers:
{text or "(no description)"}

Unit: {unit.name} (from {where}). Documents in it:
{listed or "(none found)"}

Excerpts from those documents. Base each question on one and give its id as the source:
{quoted or "(no readable documents: cover the topics in the title evenly, and leave source empty)"}

Teacher feedback on this student's earlier work:
{feedback or "(none)"}

Weak spots from this student's graded tests (worst first):
{weak or "(no graded tests yet: cover the unit's main topics evenly)"}

Question types on this class's graded tests: {", ".join(known_qtypes) or "unknown (use the formats in the excerpts)"}

Write {SET_SIZE} practice questions, easiest first."""


# ---- building the set -------------------------------------------------------------------------


def _questions(draft: PracticeDraft, picked: list[Excerpt]) -> list[PracticeQuestion]:
    """Easiest first, each tied to the excerpt it cites. Questions citing an excerpt that wasn't
    sent are dropped while enough cited ones remain."""
    by_id = {e.id: e for e in picked}
    drafts = sorted(draft.questions[:SET_SIZE], key=lambda q: q.difficulty)  # stable: keeps model order on ties
    out: list[tuple[bool, PracticeQuestion]] = []
    for q in drafts:
        ex = by_id.get(q.source.strip().strip("[]").upper())
        fields = q.model_dump(exclude={"difficulty", "source"})
        out.append((ex is not None, PracticeQuestion(**fields, source=ex.label if ex else "", source_url=ex.url if ex else "")))
    cited = [q for ok, q in out if ok]
    return cited if len(cited) >= MIN_VERIFIED else [q for _, q in out]


def _sources(card: PrepCard, docs: list[Material], picked: list[Excerpt]) -> list[SourceFile]:
    """The card's own sources, then the unit documents the set drew on and those it couldn't read."""
    used = {e.title for e in picked}
    have = {s.name for s in card.sources}
    out = list(card.sources)
    for d in docs:
        if d.kind == "feedback" or d.title in have or (d.readable and d.title not in used):
            continue
        have.add(d.title)
        out.append(SourceFile(name=d.title, url=d.url, read=d.readable, reason="" if d.readable else d.note))
    return out


def _first_step(draft: PracticeDraft, questions: list[PracticeQuestion]) -> str:
    """The model's warm-up step when question 1 is still the warm-up it was written for."""
    warmup = min(draft.questions, key=lambda q: q.difficulty, default=None)
    step = draft.first_step.strip()
    if step and warmup and questions[0].prompt == warmup.prompt and "question 1" in step.lower():
        return step
    return f"Read practice question 1 below ({questions[0].topic.replace('_', ' ')}) and write down just the formula or rule you'd use."


def add_practice(conn: sqlite3.Connection, assignment: Assignment, card: PrepCard) -> PrepCard:
    """Return the card with a practice set from the unit's materials, a focus line, the documents
    it came from, and a first step that starts question 1."""
    cells = student_model.weakest_cells(conn, assignment.course_id, n=3)
    focus = _focus_from_cells(cells)
    if not meta_client.configured():
        return card.model_copy(update={"focus": focus})

    unit = materials.find_unit(conn, assignment)
    docs = materials.gather(conn, assignment, unit)
    text, _, _ = canvas.parse_description(assignment.description_html)
    picked = materials.excerpts([d for d in docs if d.readable and d.kind != "feedback"], f"{assignment.name} {unit.name} {text}")
    log.info("practice for %s: unit %r, %d docs, %d excerpts", assignment.id, unit.name, len(docs), len(picked))
    try:
        draft = meta_client.generate_structured(
            _prompt(conn, assignment, unit, docs, picked, cells), PracticeDraft, instructions=INSTRUCTIONS
        )
        questions = _questions(draft, picked)
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

    update: dict = {"practice": questions, "focus": draft.focus or focus, "sources": _sources(card, docs, picked)}
    if picked and unit.from_module:
        update["focus"] = f"{update['focus'].rstrip()} Built from {unit.name}.".strip()
    if questions:
        update["first_step"] = _first_step(draft, questions)
    return card.model_copy(update=update)
