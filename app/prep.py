"""Prep-card pipeline: assignment (+ attachments + past feedback) -> model -> stored PrepCard.

Steps: gather the description, links and attachments (Canvas files, plus Google Docs/Drive files
that are shared publicly; DOCX converted to PDF via convert.py), ask the model for a CardDraft
with structured output while letting it call `past_feedback` and `weak_spots` (tool calling),
keep only links that really came from Canvas, add a practice set for tests, save. Without a
model key, or if the call fails, a plain template card is saved instead.

Google Assignments (an LTI tool) makes the student's copy of the doc only when it's opened from
Canvas, so there's nothing to fetch: the card's link and first step send the student there.
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel

from app import canvas, db, meta_client, practice, reader, student_model
from app.clock import as_local
from app.config import settings
from app.meta_client import MetaError, Tool
from app.models import ASSIGNMENT_KINDS, Assignment, AssignmentKind, Effort, Link, PrepCard, SourceFile

log = logging.getLogger(__name__)

INSTRUCTIONS = """You write prep cards that get a high-school student started on one assignment.
The point is to make starting effortless, not to do the work.

- first_step: ONE concrete action that takes about two minutes and needs no decisions, naming the
  exact file, page or problem (e.g. "Open the Lab 4 handout and copy the three research questions
  into a new doc titled 'Lab 4'"). Never "review the material" or "get started".
- steps: 3 to 7 short imperative steps in order, specific to this assignment, covering everything
  the rubric and instructions ask for.
- summary: one or two sentences on what has to be turned in.
- links: only links from the list you were given, with the URL copied exactly. Never invent URLs.
- If the real instructions are in a linked file you couldn't read, don't guess its contents:
  make the first step opening that file by name and doing the first thing it asks.
- watch_out: call past_feedback first. Then write ONE sentence naming a specific past mistake that
  is relevant to this assignment, quoting the teacher's words when there are any. Use "" if
  nothing relevant comes back. Never make up feedback.
- effort: light (under 45 minutes), medium, or heavy (over 2 hours or more than one sitting).
- kind: lab, reading, problem_set, essay, test, or other.
Never write any part of the answer or solve problems for the student."""


class CardDraft(BaseModel):
    """What the model returns; ids and practice questions are added by code."""

    kind: AssignmentKind
    summary: str
    first_step: str
    steps: list[str]
    links: list[Link]
    watch_out: str
    effort: Effort


# ---- inputs -----------------------------------------------------------------------------------


def uses_google_assignments(assignment: Assignment) -> bool:
    return "assignments.google.com" in assignment.external_tool_url


def turn_in(assignment: Assignment) -> str:
    """How the work gets handed in, in words the model (and student) can act on."""
    types = set(assignment.submission_types)
    if uses_google_assignments(assignment):
        return "Google Assignments: open it from the Canvas assignment page, which makes your own copy of the doc"
    if "online_quiz" in types:
        return "a Canvas quiz"
    online = [t.removeprefix("online_").replace("_", " ") for t in assignment.submission_types if t.startswith("online_")]
    if online:
        return "online in Canvas (" + ", ".join(online) + ")"
    if "external_tool" in types:
        return "through an outside tool opened from the Canvas assignment page"
    if "on_paper" in types:
        return "on paper"
    if "not_graded" in types:
        return "not graded (preparation or reading)"
    return "not stated (often done or handed in during class)"


def known_links(assignment: Assignment) -> list[Link]:
    """Every link the card may show: those in the description, plus the assignment itself."""
    _, links, _ = canvas.parse_description(assignment.description_html)
    if assignment.html_url:
        label = "Open in Google Assignments (via Canvas)" if uses_google_assignments(assignment) else "Assignment on Canvas"
        links.append(Link(label=label, url=assignment.html_url))
    return links


def _attachments(assignment: Assignment) -> tuple[list[Path], list[SourceFile]]:
    """Read every file and doc the description points at (reader.py: Canvas files, Google files
    linked or embedded, Canvas pages, web articles) and pick the ones to send the model.

    Returns (files for the model, one SourceFile per document tried). The read=True sources line
    up, in order, with the files; the others say why they couldn't be opened or weren't sent."""
    docs = reader.read_assignment(assignment)
    files, sent = reader.model_files(docs)
    sent_ids = {id(d) for d in sent}
    sources = []
    for d in docs:
        if id(d) in sent_ids:
            sources.append(d.source().model_copy(update={"read": True, "reason": ""}))
        elif d.readable:
            sources.append(d.source().model_copy(update={"read": False, "reason": "not sent: too many files for one card"}))
        else:
            sources.append(d.source())
    return files, sources


def _due_text(assignment: Assignment) -> str:
    if not assignment.due_at:
        return "no due date"
    return as_local(assignment.due_at).strftime("%A %b %d, %I:%M %p")


def _prompt(
    assignment: Assignment,
    course_name: str,
    text: str,
    links: list[Link],
    files: list[Path],
    unreadable: list[Link] | None = None,
) -> str:
    rubric = "\n".join(
        f"- {r.criterion} ({r.points_possible:g} pts)" if r.points_possible is not None else f"- {r.criterion}"
        for r in assignment.rubric
    )
    link_lines = "\n".join(f"- {l.label}: {l.url}" for l in links) or "(none)"
    blocked = "\n".join(f"- {l.label}" for l in unreadable or []) or "(none)"
    return f"""Course: {course_name}
Assignment: {assignment.name}
Kind (guessed from the title): {assignment.kind}
Due: {_due_text(assignment)}
Points: {assignment.points_possible if assignment.points_possible is not None else "unknown"}
How it's turned in: {turn_in(assignment)}
Rubric criteria:
{rubric or "(no rubric)"}

Links you may use:
{link_lines}

Attached files (you can read these): {", ".join(f.name for f in files) or "(none)"}
Linked files you can't read (shared only inside the school):
{blocked}

Instructions from the teacher:
{text or "(no description; work from the title and any attached files)"}"""


def _tools(conn: sqlite3.Connection, assignment: Assignment) -> list[Tool]:
    course_id = assignment.course_id
    return [
        Tool(
            name="past_feedback",
            description=(
                "This student's graded work in the same course: rubric criteria where they lost "
                "points (with the teacher's comment), teacher comments, and missed test questions. "
                "Filter by assignment kind, or 'any'."
            ),
            parameters={
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": [*ASSIGNMENT_KINDS, "any"]}},
                "required": ["kind"],
                "additionalProperties": False,
            },
            fn=lambda args: student_model.feedback_evidence(
                conn, course_id, None if args.get("kind") in (None, "any") else args["kind"]
            ),
        ),
        Tool(
            name="weak_spots",
            description=(
                "Topic and question-type combinations where this student loses the most marks on "
                "graded tests in this course, with example missed questions."
            ),
            parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            fn=lambda args: student_model.weakest_cells(conn, course_id, n=5),
        ),
    ]


# ---- cards ------------------------------------------------------------------------------------

_FALLBACK_FIRST_STEP: dict[str, str] = {
    "lab": "Open the lab handout and copy its section headings into a new doc titled '{name}'.",
    "reading": "Open the reading and read only the first page, then write one sentence on what it's saying.",
    "problem_set": "Open the problem set and copy problem 1 onto paper, underlining what it asks for.",
    "essay": "Open a new doc titled '{name}' and write a one-sentence answer to the prompt, even a rough one.",
    "test": "Write down, from memory, the three topics you think this test covers.",
    "other": "Open the assignment page and write down, in one line, what you have to turn in.",
}

_FALLBACK_STEPS: dict[str, list[str]] = {
    "lab": ["Read the procedure once", "Set up the data table", "Write the observations section", "Draft the conclusion", "Check the rubric before submitting"],
    "reading": ["Read the assigned section", "Mark two passages worth quoting", "Write your response", "Check the prompt is fully answered"],
    "problem_set": ["Do problems in order, showing every step", "Mark any you're stuck on", "Check answers where possible", "Submit"],
    "essay": ["Write a one-line thesis", "Outline three body paragraphs with evidence", "Draft", "Revise against the rubric"],
    "test": ["List the topics covered", "Do practice questions on your weakest topic", "Review mistakes", "Do one timed mini-set"],
    "other": ["Read the instructions fully", "List what you need to turn in", "Do the first part", "Check and submit"],
}


def fallback_card(conn: sqlite3.Connection, assignment: Assignment) -> PrepCard:
    """A template card from the assignment's kind, used when the model isn't available."""
    text, _, _ = canvas.parse_description(assignment.description_html)
    summary = " ".join(text.split())[:240] or assignment.name
    first_step = _FALLBACK_FIRST_STEP[assignment.kind].format(name=assignment.name)
    if uses_google_assignments(assignment):
        first_step = "Open the assignment on Canvas and click through to Google Assignments to open your copy of the doc."
    return PrepCard(
        assignment_id=assignment.id,
        kind=assignment.kind,
        summary=summary,
        first_step=first_step,
        steps=_FALLBACK_STEPS[assignment.kind],
        links=known_links(assignment),
        watch_out=student_model.watch_out_line(conn, assignment),
        generated_by="fallback",
    )


def _model_card(conn: sqlite3.Connection, assignment: Assignment) -> PrepCard:
    courses = {c.id: c.name for c in db.list_courses(conn)}
    text, _, _ = canvas.parse_description(assignment.description_html)
    links = known_links(assignment)
    files, sources = _attachments(assignment)
    unreadable = [Link(label=f"{s.name} ({s.reason})", url=s.url) for s in sources if not s.read]
    draft = meta_client.generate_structured(
        _prompt(assignment, courses.get(assignment.course_id, ""), text, links, files, unreadable),
        CardDraft,
        files=files,
        instructions=INSTRUCTIONS,
        tools=_tools(conn, assignment),
    )
    allowed = {l.url for l in links}
    kept = [l for l in draft.links if l.url in allowed]
    return PrepCard(
        assignment_id=assignment.id,
        kind=assignment.kind if assignment.kind != "other" else draft.kind,
        summary=draft.summary,
        first_step=draft.first_step,
        steps=draft.steps[:7],
        links=kept or links,
        watch_out=draft.watch_out or student_model.watch_out_line(conn, assignment),
        effort=draft.effort,
        generated_by="model",
        sources=sources,
    )


def build_prep_card(conn: sqlite3.Connection, assignment: Assignment) -> PrepCard:
    """Generate and store the card for one assignment."""
    card = None
    if meta_client.configured():
        try:
            card = _model_card(conn, assignment)
        except (MetaError, httpx.HTTPError) as e:
            log.warning("model card for %s failed, using template: %s", assignment.id, e)
    if card is None:
        card = fallback_card(conn, assignment)
    if card.kind == "test":
        card = practice.add_practice(conn, assignment, card)
    db.save_card(conn, card)
    return card


def upcoming(assignments: list[Assignment], now: datetime, horizon_days: int) -> list[Assignment]:
    """Unsubmitted assignments due between now and the horizon, soonest first."""
    end = now + timedelta(days=horizon_days)
    out = [a for a in assignments if not a.submitted and a.due_at and now < a.due_at <= end]
    return sorted(out, key=lambda a: a.due_at)


def build_all_missing(
    conn: sqlite3.Connection, now: datetime, horizon_days: int | None = None, limit: int | None = None
) -> list[PrepCard]:
    """Generate cards for upcoming assignments that don't have one yet (soonest due first).

    Model cards cost money, so they're capped per call; template cards are free and uncapped.
    """
    have = {c.assignment_id for c in db.list_cards(conn)}
    todo = [
        a for a in upcoming(db.list_assignments(conn), now, horizon_days or settings.card_horizon_days)
        if a.id not in have
    ]
    if meta_client.configured():
        todo = todo[: limit or settings.max_cards_per_sync]
    return [build_prep_card(conn, a) for a in todo]
