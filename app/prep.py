"""Prep-card pipeline: assignment (+ attachments + past feedback) -> model -> stored PrepCard.

Steps: gather the description, links and attachments (Canvas files, plus Google Docs/Drive files
that are shared publicly; DOCX converted to PDF via convert.py), ask the model for a CardDraft
with structured output while letting it call `past_feedback` and `weak_spots` (tool calling),
keep only links that really came from Canvas, add a practice set for tests, save. Without a
model key, or if the call fails, a plain template card is saved instead.

Code, not the model, has the last word on what it can check: the kind (a Canvas quiz is always a
test), the watch-out (its quotes must appear in actionable past feedback that fits this
assignment, or in the instructions), and a reading's length and stopping points (reading.py,
from the file's text). Where the student keeps notes is a per-course setting ("paper" or "doc");
unset, cards say "your notes".

Google Assignments (an LTI tool) makes the student's copy of the doc only when it's opened from
Canvas, so there's nothing to fetch: the card's link and first step send the student there.
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from app import canvas, db, meta_client, practice, reader, reading, student_model
from app.clock import as_local
from app.config import settings
from app.meta_client import MetaError
from app.models import (
    Assignment,
    AssignmentKind,
    Effort,
    Link,
    PrepCard,
    ReadingPlan,
    SourceFile,
)

log = logging.getLogger(__name__)


INSTRUCTIONS = """You write prep cards that get a high-school student started on one assignment.
The point is to make starting effortless, not to do the work.

- kind: decide it from the instructions and files. The title guess you're given is only a hint
  and is often wrong. test only when the student will sit a quiz, test or exam for this item.
  Practice problems, review sheets, study guides and corrections are problem_set, even when the
  title says test or exam. Otherwise lab, reading, essay, or other.
- first_step: ONE physical action that takes about two minutes and needs no thinking: opening,
  finding, copying, labeling, or reading a few lines. Name the exact file, page, problem or
  section (e.g. "Open the Lab 4 handout and copy its three research questions to the top of your
  notes"). It must not ask the student to solve, answer, analyze, summarize or decide anything.
  Never "review the material" or "get started".
- Where the student writes: follow the Notes line in the prompt. Don't pick a doc or paper yourself.
- steps: 3 to 7 short imperative steps in order, specific to this assignment, covering everything
  the rubric and instructions ask for.
- summary: one or two sentences on what has to be turned in.
- links: only links from the list you were given, with the URL copied exactly. Never invent URLs.
- Only name specifics (headings, data tables, problem numbers, page numbers, materials,
  procedures) that appear in the instructions or files you can read. If the instructions aren't
  available, or are in a file you couldn't read, say so in the summary, make the first step
  finding and opening that handout by name, and keep the steps to what any such assignment needs.
- Readings: when a Reading plan is given and this is a reading, its sittings become the reading
  steps. Then put in steps only what to do besides reading (notes, questions to prepare), at most
  3, and in reading_notes ONE short note per sitting, in order, on what to look for in it (tied to
  the teacher's questions when there are any). The first step starts sitting 1 at its first page.
  Otherwise reading_notes is [].
- watch_out: ONE sentence naming a specific mistake from the Past feedback list that this
  assignment could repeat: an item from the same kind of work, or one sharing a rubric criterion.
  Quote the teacher's criticism in double quotes. Never use praise, grades, or notes about where
  to find feedback. If nothing fits, you may instead quote a warning the teacher wrote in these
  instructions. Otherwise "". Quote exactly; never invent or reword anything inside quotes.
- effort: light (under 45 minutes), medium, or heavy (over 2 hours or more than one sitting).
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
    reading_notes: list[str] = Field(default_factory=list)  # one per reading sitting


# ---- per-course notes setting -----------------------------------------------------------------

NOTES_MEDIA = ("paper", "doc")

_NOTES_LINE = {
    "paper": "on paper. Say 'on paper' or 'in your notebook'; never a doc or a file.",
    "doc": "in a doc on the computer. Say 'in your notes doc'; never paper.",
    None: "not set. Say 'your notes', not a doc or paper, unless the instructions say which.",
}


def notes_medium(conn: sqlite3.Connection, course_id: int) -> str | None:
    """'paper', 'doc', or None when the student hasn't said."""
    value = db.kv_get(conn, f"notes_medium:{course_id}")
    return value if value in NOTES_MEDIA else None


def set_notes_medium(conn: sqlite3.Connection, course_id: int, medium: str | None) -> None:
    if medium is not None and medium not in NOTES_MEDIA:
        raise ValueError(f"notes medium must be one of {NOTES_MEDIA} or null")
    db.kv_set(conn, f"notes_medium:{course_id}", medium)


def decide_kind(assignment: Assignment, model_kind: AssignmentKind) -> AssignmentKind:
    """The card's kind: a Canvas quiz is a test; otherwise the model's read of the instructions
    (the title guess was only a hint in its prompt)."""
    if "online_quiz" in assignment.submission_types:
        return "test"
    return model_kind


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
    notes: str | None = None,
    plan: ReadingPlan | None = None,
    feedback: str = "(none)",
    read_names: list[str] | None = None,
) -> str:
    rubric = "\n".join(
        f"- {r.criterion} ({r.points_possible:g} pts)" if r.points_possible is not None else f"- {r.criterion}"
        for r in assignment.rubric
    )
    link_lines = "\n".join(f"- {l.label}: {l.url}" for l in links) or "(none)"
    blocked = "\n".join(f"- {l.label}" for l in unreadable or []) or "(none)"
    if not text.strip() and not files:
        names = ", ".join(f"'{l.label}'" for l in unreadable or [])
        seen = (
            "NO. The description is empty and no file could be read"
            + (f" (the handout is probably {names}, on the Canvas assignment page)" if names else "")
            + ". Don't guess what the instructions say."
        )
    else:
        seen = "yes"
    reading_plan = (
        "Reading plan (worked out from the file's text; use only if this is a reading):\n" + reading.prompt_block(plan)
        if plan
        else "Reading plan: (none)"
    )
    return f"""Course: {course_name}
Assignment: {assignment.name}
Kind guessed from the title only (often wrong): {assignment.kind}
Due: {_due_text(assignment)}
Points: {assignment.points_possible if assignment.points_possible is not None else "unknown"}
How it's turned in: {turn_in(assignment)}
Notes for this course: {_NOTES_LINE[notes]}
Rubric criteria:
{rubric or "(no rubric)"}

Links you may use:
{link_lines}

Attached files (you can read these, in this order): {"; ".join(read_names or [f.name for f in files]) or "(none)"}
Linked files you can't read:
{blocked}
Instructions available: {seen}

{reading_plan}

Past feedback in this course (actionable criticism only; [kind] is the kind of work it came from):
{feedback}

Instructions from the teacher:
{text or "(no description)"}"""


def _feedback_block(conn: sqlite3.Connection, assignment: Assignment) -> str:
    """This student's actionable past feedback in the course, for the prompt. Sent up front rather
    than as a tool: a tool call costs a second round with every file re-sent."""
    lines = []
    for item in student_model.feedback_evidence(conn, assignment.course_id, limit=12):
        if item.get("assignment_id") == assignment.id:
            continue
        if item["source"] == "rubric":
            said = f' Teacher: "{item["teacher_comment"]}"' if item["teacher_comment"] else ""
            lines.append(
                f"- [{item['kind']}] {item['assignment']}, rubric \"{item['criterion']}\": lost "
                f"{item['points_lost']:g}/{item['points_possible']:g}.{said}"
            )
        elif item["source"] == "comment":
            lines.append(f"- [{item['kind']}] {item['assignment']}, {item['author'] or 'teacher'}: \"{item['text']}\"")
        else:
            note = f' Teacher: "{item["teacher_note"]}"' if item["teacher_note"] else ""
            lines.append(f"- [test] graded test, {item['topic']} / {item['qtype']}: missed \"{item['question']}\".{note}")
    return "\n".join(lines) or "(none)"


# ---- cards ------------------------------------------------------------------------------------

_FALLBACK_FIRST_STEP: dict[str, str] = {
    "lab": "Open the lab handout and copy its section headings to {where}.",
    "reading": "Open the reading to its first page and read just the first two paragraphs.",
    "problem_set": "Open the problem set and copy problem 1 to {where}.",
    "essay": "Copy the essay prompt, word for word, to {where}.",
    "test": "Write '{name}' at {where}, then open your class notes for this unit to the first page.",
    "other": "Open the assignment on Canvas and copy its first instruction to {where}.",
}

_WHERE = {
    "paper": "the top of a fresh page in your notebook, titled '{name}'",
    "doc": "a new doc titled '{name}'",
    None: "the top of your notes for '{name}'",
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
    where = _WHERE[notes_medium(conn, assignment.course_id)].format(name=assignment.name)
    first_step = _FALLBACK_FIRST_STEP[assignment.kind].format(name=assignment.name, where=where)
    seen = bool(text.strip())
    if not seen:
        summary = f"{assignment.name}: the instructions weren't readable here, so this card is from the title only."
        first_step = "Open the assignment on Canvas and find the handout or instructions on that page, then open them."
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
        instructions_seen=seen,
    )


def _reading_plan(docs: list[reader.Doc], text: str) -> ReadingPlan | None:
    """A plan for the longest readable document, limited to a page range the instructions name."""
    readable = [d for d in docs if d.readable and d.book_pages]
    if not readable:
        return None
    doc = max(readable, key=lambda d: sum(len(p.text.split()) for p in d.book_pages))
    return reading.plan(doc.book_pages, doc.name, reading.page_range(text))


def _reading_effort(plan: ReadingPlan) -> Effort:
    if len(plan.sittings) > 1 or plan.minutes > 120:
        return "heavy"
    return "light" if plan.minutes < 45 else "medium"


def _watch_out(conn: sqlite3.Connection, assignment: Assignment, kind: str, line: str, text: str) -> str:
    """The model's line if its quotes check out against fitting evidence or the instructions,
    else the student model's own line (which may be '')."""
    evidence = student_model.relevant_evidence(conn, assignment, kind, limit=20)
    sources = [text] + [
        item[key] for item in evidence
        for key in ("text", "teacher_comment", "criterion", "teacher_note", "question")
        if item.get(key)
    ]
    if line and student_model.quote_checked(line, sources):
        return line
    if line:
        log.info("dropped watch-out for %s (quote not found in fitting feedback): %s", assignment.id, line)
    return student_model.watch_out_line(conn, assignment, kind)


def _model_card(conn: sqlite3.Connection, assignment: Assignment) -> PrepCard:
    courses = {c.id: c.name for c in db.list_courses(conn)}
    text, _, _ = canvas.parse_description(assignment.description_html)
    links = known_links(assignment)
    got = _attachments(assignment)
    files, sources = got[0], got[1]
    docs = got[2] if len(got) > 2 else [reader.read_document(f) for f in files]
    unreadable = [Link(label=f"{s.name} ({s.reason})", url=s.url) for s in sources if not s.read]
    plan = _reading_plan(docs, text)
    draft = meta_client.generate_structured(
        _prompt(
            assignment, courses.get(assignment.course_id, ""), text, links, files, unreadable,
            notes=notes_medium(conn, assignment.course_id), plan=plan,
            feedback=_feedback_block(conn, assignment), read_names=[s.name for s in sources if s.read],
        ),
        CardDraft,
        files=files,
        instructions=INSTRUCTIONS,
    )
    kind = decide_kind(assignment, draft.kind)
    steps, effort = draft.steps[:7], draft.effort
    if kind == "reading" and plan:
        notes = draft.reading_notes + [""] * len(plan.sittings)
        steps = [reading.step(s, n) for s, n in zip(plan.sittings, notes)] + draft.steps[:3]
        effort = _reading_effort(plan)
    else:
        plan = None
    allowed = {l.url for l in links}
    kept = [l for l in draft.links if l.url in allowed]
    return PrepCard(
        assignment_id=assignment.id,
        kind=kind,
        summary=draft.summary,
        first_step=draft.first_step,
        steps=steps,
        links=kept or links,
        watch_out=_watch_out(conn, assignment, kind, draft.watch_out, text),
        effort=effort,
        generated_by="model",
        sources=sources,
        instructions_seen=bool(text.strip() or files),
        reading=plan,
    )


def build_prep_card(conn: sqlite3.Connection, assignment: Assignment) -> PrepCard:
    """Generate and store the card for one assignment. If the model call fails, the template card
    is saved with the reason in `model_error`."""
    card = None
    error = ""
    if meta_client.configured():
        try:
            card = _model_card(conn, assignment)
        except (MetaError, httpx.HTTPError) as e:
            log.warning("model card for %s failed, using template: %s", assignment.id, e)
            error = f"{type(e).__name__}: {e}"[:300]
    if card is None:
        card = fallback_card(conn, assignment).model_copy(update={"model_error": error})
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
