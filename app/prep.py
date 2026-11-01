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
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from app import canvas, db, materials, meta_client, practice, reader, reading, student_model
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
from app.progress import Progress

log = logging.getLogger(__name__)

CARD_WORKERS = 5  # model cards built at once during a sync (= the default MAX_CARDS_PER_SYNC)

INSTRUCTIONS = """You write prep cards that get a high-school student started on one assignment.
The point is to make starting effortless, not to do the work.

- kind: decide it from the instructions and files. The title guess you're given is only a hint
  and is often wrong. test only when the student will sit a quiz, test or exam for this item.
  Practice problems, review sheets, study guides and corrections are problem_set, even when the
  title says test or exam. Otherwise lab, reading, essay, or other.
- first_step: ONE physical action that takes about two minutes and needs no thinking: opening,
  finding, copying, labeling, or reading a few lines. Name the exact file, page, problem or
  section. Make it the first real piece of the work, shrunk: "Open the Lab 4 handout and read
  step 1 of the procedure", "Write out problem 1 of the worksheet", "Read the first stanza of
  'A Litany for Survival'". Copying titles or headings is a ritual, not a start. It must not ask
  the student to solve, answer, analyze, summarize or decide anything. Never "review the
  material" or "get started".
- Where the student writes: follow the Notes line in the prompt, and name the place at most once
  on the whole card, in the first step that has the student write. Never attach it to watching,
  reading, opening, checking or bringing ("watch the video in your notes" is nonsense).
- Files and links: call them by what they are ("the answer key", "the lab handout", "the chapter
  PDF", "the Khan Academy video"), or by their title when it is a real name ("Test Review:
  Vectors"). Never paste link text that is a phrase ("KEY is provided here", "click here") into a
  sentence.
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
  3, and in reading_notes ONE note per sitting, in order: the one thing to look for in THAT
  sitting's pages (its sections and their opening words are listed), tied to the teacher's
  questions where they fit. Notes follow the pages, not the questions: a question may come up in
  several sittings or none. Each note is one plain clause of at most 12 words that starts "Look
  for", with no list of names or examples and no pages, e.g. "Look for how Canada became a
  destination for freedom seekers." The first step starts sitting 1 at its first page. Otherwise
  reading_notes is [].
- Lists in the instructions (discussion questions, prompts, problems to answer): a step that uses
  them names them with their count and the teacher's word for them ("the three discussion
  questions"). The card shows the list under that step, so never copy the items into a step.
- Tests: when Unit materials are given and this is a test, every step names a unit document by its
  title and, where the outline lists them, the exact problems, pages or slides, with their topic:
  "Redo Test Review: Vectors problems 10-14 (relative motion)", "Re-read Presentation: Vectors &
  Relative Motion (Part 2) pp. 9-15 (relative velocity examples)", "Do the practice set below,
  then check what you missed against Test Review: Vectors KEY". Weight the steps toward the
  student's weak topics in Past feedback. Never "gather your notes", "review the material" or
  "recheck the assignment".
- watch_out: ONE sentence naming a specific mistake from the Past feedback list that this
  assignment could repeat: an item from the same kind of work, or one sharing a rubric criterion.
  Quote the teacher's criticism in double quotes. Never use praise, grades, or notes about where
  to find feedback. If nothing fits, quote instead the one requirement or warning in the teacher's
  instructions or handout that is easiest to miss and would cost points (showing every step,
  units, a length or format rule, what must be turned in), e.g. 'The handout asks you to "show
  all of your steps" for every calculation.' Never generic advice. If there is none, "". Quote
  exactly; never invent or reword anything inside quotes.
- effort: light (under 45 minutes), medium, or heavy (over 2 hours or more than one sitting).
- quick: true only when the whole assignment is one small action of about 10 minutes with nothing
  to prepare, read, write or solve: filling in a form or survey, signing or acknowledging
  something, turning in work that is already finished. Then first_step is that action, naming the
  form or page; steps is []; effort is light. Quizzes, tests, exit tickets, checks for
  understanding, readings and anything with questions to answer are never quick. Otherwise false.
- Answer keys (listed in the prompt) are only for checking finished work. A step may say to check
  against the key by name; never put its answers, values or worked steps anywhere on the card.
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
    quick: bool = False


# ---- per-course notes setting -----------------------------------------------------------------

NOTES_MEDIA = ("paper", "doc")

_NOTES_LINE = {
    "paper": "on paper. Where writing first happens, say 'in your notebook' once; never a doc or a file.",
    "doc": "in a doc on the computer. Where writing first happens, say 'in your notes doc' once; never paper.",
    None: "not set. Don't say where to write (no 'in your notes', doc or paper) unless the instructions say; "
    "just say what to write.",
}

# "in your notes", "to your notebook", "in your notes doc": where the student writes
_PLACE = re.compile(r",?\s+(?:in|into|to|on) your (?:notes(?: doc)?|notebook)\b", re.I)
_WRITES = re.compile(
    r"^\W*(?:write|copy|jot|note|list|draw|record|answer|solve|do|redo|rework|work|make|label|sketch|"
    r"paraphrase|summari[sz]e|outline|draft|fill|take|calculate|show|finish|complete|set up)\b",
    re.I,
)


def _place_once(lines: list[str], allowed: int) -> list[str]:
    """Keep "in your notes" (or notebook / notes doc) on at most `allowed` lines, and only on a line
    that starts with writing and doesn't already name the notes ("from your notes in your notes")."""
    out = []
    for line in lines:
        def keep(m: re.Match) -> str:
            nonlocal allowed
            rest = (line[: m.start()] + line[m.end():]).lower()
            if allowed and _WRITES.match(line) and "your notes" not in rest and "your notebook" not in rest:
                allowed -= 1
                return m.group(0)
            return ""
        out.append(_PLACE.sub(keep, line))
    return out


def notes_medium(conn: sqlite3.Connection, course_id: int) -> str | None:
    """'paper', 'doc', or None when the student hasn't said."""
    value = db.kv_get(conn, f"notes_medium:{course_id}")
    return value if value in NOTES_MEDIA else None


def set_notes_medium(conn: sqlite3.Connection, course_id: int, medium: str | None) -> None:
    if medium is not None and medium not in NOTES_MEDIA:
        raise ValueError(f"notes medium must be one of {NOTES_MEDIA} or null")
    db.kv_set(conn, f"notes_medium:{course_id}", medium)


def mark_course_stale(conn: sqlite3.Connection, course_id: int, now: datetime) -> list[int]:
    """After the course's notes setting changes: mark its upcoming cards stale so the next sync
    rebuilds them once (build_all_missing). Returns the ids of the cards that will be rebuilt."""
    due = {a.id for a in upcoming(db.list_assignments(conn), now, settings.card_horizon_days) if a.course_id == course_id}
    rebuilt = []
    for card in db.list_cards(conn):
        if card.assignment_id in due:
            if not card.stale:
                db.save_card(conn, card.model_copy(update={"stale": True}))
            rebuilt.append(card.assignment_id)
    return rebuilt


def has_writing(card: PrepCard) -> bool:
    """Whether the card has the student write something (so it's worth asking where notes live)."""
    return any(_WRITES.match(s) for s in [card.first_step, *card.steps])


def decide_kind(assignment: Assignment, model_kind: AssignmentKind) -> AssignmentKind:
    """The card's kind: a Canvas quiz is a test; otherwise the model's read of the instructions
    (the title guess was only a hint in its prompt)."""
    if "online_quiz" in assignment.submission_types:
        return "test"
    return model_kind


# ---- lists from the instructions, under the step that uses them --------------------------------

_LIST_NOUN = re.compile(r"\b(questions?|prompts?|problems?|tasks?|topics?|requirements?|terms?|parts?)\b", re.I)
_NUMBERED = re.compile(r"^\(?\d{1,2}[.)]\s+")
_COUNT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
_FILLER = set(
    "the a an and or of to in on for with as at by from this that these those your you be are is it its "
    "will want each every own into about keep".split()
)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in _FILLER}


def instruction_lists(description_html: str) -> list[tuple[str, list[str]]]:
    """Each bulleted or numbered list in the teacher's instructions with the paragraph that
    introduces it: [("Keep these discussion questions in mind as you read. ...", [q1, q2, q3])]."""
    text, _, _ = canvas.parse_description(description_html)
    out: list[tuple[str, list[str]]] = []
    lead, items = "", []
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        if para.startswith("- ") or _NUMBERED.match(para):
            items.append(_NUMBERED.sub("", para.removeprefix("- ")).strip())
            continue
        if len(items) >= 2:
            out.append((lead, items))
        lead, items = para, []
    if len(items) >= 2:
        out.append((lead, items))
    return out


def step_lists(steps: list[str], description_html: str) -> list[dict]:
    """The teacher's lists, each under the one step that refers to it, for the card to show
    there: [{"step": 3, "label": "the 3 discussion questions", "items": [...]}].

    A step refers to a list when it names the same kind of thing as the list's lead-in ("the
    three discussion questions" for "Keep these discussion questions in mind"). Among those, the
    step sharing the most words with the lead-in wins, plus a point for saying how many there are."""
    out = []
    for lead, items in instruction_lists(description_html):
        nouns = {m.lower().rstrip("s") for m in _LIST_NOUN.findall(lead)}
        lead_words = _words(lead)
        n = len(items)
        says_count = re.compile(rf"\b(?:{n}|{_COUNT_WORDS[n] if n < len(_COUNT_WORDS) else n})\b", re.I)
        best, best_score, best_noun = None, 0, ""
        for i, s in enumerate(steps):
            found = [m for m in _LIST_NOUN.finditer(s) if m.group(1).lower().rstrip("s") in nouns]
            if not found:
                continue
            score = len(_words(s) & lead_words) + (1 if says_count.search(s) else 0)
            if score > best_score:
                best, best_score, best_noun = i, score, s[: found[0].end()]
        if best is None:
            continue
        # "the 3 discussion questions": the noun, with the word before it when the lead-in has it too
        before = re.findall(r"[A-Za-z]+", best_noun)
        noun = before[-1].lower()
        adjective = before[-2].lower() if len(before) > 1 and before[-2].lower() in lead_words else ""
        label = " ".join(w for w in ["the", str(n), adjective, noun if noun.endswith("s") else noun + "s"] if w)
        out.append({"step": best, "label": label, "items": items})
    return out


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


def _attachments(assignment: Assignment) -> tuple[list[Path], list[SourceFile], list[reader.Doc]]:
    """Read every file and doc the description points at (reader.py: Canvas files, Google files
    linked or embedded, Canvas pages, web articles) and pick the ones to send the model.

    Returns (files for the model, one SourceFile per document tried, the Docs sent). The read=True
    sources and the Docs line up, in order, with the files; the other sources say why they couldn't
    be opened or weren't sent. A Doc keeps its book pages even when its file went as text. Scans
    that get sent are transcribed first (once per file), so they have text like any other doc."""
    docs = reader.read_scans(reader.read_assignment(assignment))
    files, sent = reader.model_files(docs)
    sent_ids = {id(d) for d in sent}
    sources = []
    for d in docs:
        if id(d) in sent_ids:
            sources.append(d.source().model_copy(update={"read": True, "reason": reader.read_note(d)}))
        elif d.readable:
            sources.append(d.source().model_copy(update={"read": False, "reason": "not sent: too many files for one card"}))
        else:
            sources.append(d.source())
    return files, sources, sent


def _due_text(assignment: Assignment) -> str:
    if not assignment.due_at:
        return "no due date"
    return as_local(assignment.due_at).strftime("%A %b %d, %I:%M %p")


# "KEY: Unit Vectors Practice", "Test Review KEY", "Key - Lab 3", "Answer Key", "Answers to the
# worksheet". "Key" inside a title isn't enough ("Key Concepts"), nor is "solutions" (chemistry).
_KEY_WORD = re.compile(r"\bKEY\b|(?i:^\s*key\s*[:\-–])")
_ANSWER_KEY = re.compile(r"\banswer\s+(?:key|sheet)s?\b|\banswers\s+(?:to|for)\b|\bsolutions?\s+(?:key|manual|to|for)\b", re.I)


def is_answer_key(name: str) -> bool:
    """Whether a file's name says it's an answer key (the card may point at it, never copy from it)."""
    return bool(_KEY_WORD.search(name) or _ANSWER_KEY.search(name))


_PHRASE = re.compile(r"\b(?:here|is|are|provided|click|this|these|link|below|above|attached)\b", re.I)


def _name_keys(lines: list[str], labels: list[str]) -> list[str]:
    """Replace an answer key's link text, when it is a phrase ("KEY is provided here"), with what it
    is: "check against the KEY is provided here" -> "check against the answer key"."""
    phrases = sorted((l for l in labels if is_answer_key(l) and _PHRASE.search(l)), key=len, reverse=True)
    for label in phrases:
        pattern = re.compile(r"\b(?:the\s+)?" + re.escape(label), re.I)
        lines = [pattern.sub("the answer key", line) for line in lines]
    return lines


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
    unit: str = "",
) -> str:
    rubric = "\n".join(
        f"- {r.criterion} ({r.points_possible:g} pts)" if r.points_possible is not None else f"- {r.criterion}"
        for r in assignment.rubric
    )
    key = lambda label: " [the answer key]" if is_answer_key(label) else ""
    link_lines = "\n".join(f"- {l.label}{key(l.label)}: {l.url}" for l in links) or "(none)"
    blocked = "\n".join(f"- {l.label}{key(l.label)}" for l in unreadable or []) or "(none)"
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
    read_names = read_names or [f.name for f in files]
    keys = [n for n in read_names if is_answer_key(n)]
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

Attached files (you can read these, in this order): {"; ".join(read_names) or "(none)"}
Answer keys among them (for checking finished work only; copy nothing from them): {"; ".join(keys) or "(none)"}
Linked files you can't read:
{blocked}
Instructions available: {seen}

{reading_plan}

Unit materials for this test (use only if this is a test):
{unit or "(none)"}

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
    "lab": "Open the {name} handout and read step 1 of the procedure.",
    "reading": "Open the reading to its first page and read just the first two paragraphs.",
    "problem_set": "Open the problem set and copy problem 1 to {where}.",
    "essay": "Copy the essay prompt, word for word, to {where}.",
    "test": "Write '{name}' at {where}, then open your class notes for this unit to the first page.",
    "other": "Open the assignment on Canvas and copy its first instruction to {where}.",
}

_WHERE = {
    "paper": "the top of a fresh page in your notebook, titled '{name}'",
    "doc": "a new doc titled '{name}'",
    None: "the top of a blank page",
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


_READ_PAGES = re.compile(r"^\s*read\b.*\bpp?\.\s*\d", re.I)  # "Read pp. 177-187 ..."
_NOTE_PREFIX = re.compile(r"^\s*(?:sitting\s*\d+\s*)?(?:\(?pp?\.\s*[\d\s–-]+\)?\s*)?[:.\-–]\s*", re.I)  # "Sitting 1 pp. 177-187: "


def _reading_effort(plan: ReadingPlan) -> Effort:
    if len(plan.sittings) > 1 or plan.minutes > 120:
        return "heavy"
    return "light" if plan.minutes < 45 else "medium"


def _watch_out(
    conn: sqlite3.Connection, assignment: Assignment, kind: str, line: str, text: str, handouts: list[str] = ()
) -> str:
    """The model's line if its quotes check out against fitting evidence, the instructions or the
    files it read (a requirement easy to miss), else the student model's own line (which may be '')."""
    evidence = student_model.relevant_evidence(conn, assignment, kind, limit=20)
    sources = [text, *handouts] + [
        item[key] for item in evidence
        for key in ("text", "teacher_comment", "criterion", "teacher_note", "question")
        if item.get(key)
    ]
    if line and student_model.quote_checked(line, sources):
        return line
    if line:
        log.info("dropped watch-out for %s (quote not found in fitting feedback): %s", assignment.id, line)
    return student_model.watch_out_line(conn, assignment, kind)


def _unit_outline(conn: sqlite3.Connection, assignment: Assignment) -> str:
    """For a test (by its title guess or a Canvas quiz): the unit's documents and what's in each,
    so the steps can name exact problems and slides. Answer keys keep only their title line."""
    if assignment.kind != "test" and "online_quiz" not in assignment.submission_types:
        return ""
    try:
        unit, docs = materials.unit_outline(conn, assignment)
    except Exception as e:  # Canvas, Google or a file read failed: the card goes without the outline
        log.warning("unit outline for %s failed: %s", assignment.id, e)
        return ""
    docs = [d.model_copy(update={"parts": []}) if d.answer_key else d for d in docs]
    return materials.outline_text(unit, docs) if docs else ""


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
            unit=_unit_outline(conn, assignment),
        ),
        CardDraft,
        files=files,
        instructions=INSTRUCTIONS,
    )
    kind = decide_kind(assignment, draft.kind)
    steps, effort = draft.steps[:7], draft.effort
    quick = draft.quick and kind != "test"  # a test always gets its practice
    if quick:
        steps, effort, plan = [], "light", None
    elif kind == "reading" and plan:
        notes = [_NOTE_PREFIX.sub("", n) for n in draft.reading_notes]
        notes = [n[:1].upper() + n[1:] for n in notes] + [""] * len(plan.sittings)
        extra = [s for s in draft.steps if not _READ_PAGES.match(s)][:3]  # the plan already has these
        steps = [reading.step(s, n) for s, n in zip(plan.sittings, notes)] + extra
        effort = _reading_effort(plan)
    else:
        plan = None
    medium = notes_medium(conn, assignment.course_id)
    says_notes = bool(re.search(r"\bnotes?\b|\bnotebook\b", text, re.I))
    first_step, *steps = _place_once([draft.first_step, *steps], 1 if medium or says_notes else 0)
    labels = [l.label for l in links] + [s.name for s in sources]
    summary, first_step, *steps = _name_keys([draft.summary, first_step, *steps], labels)
    same = lambda s: " ".join(s.lower().rstrip(". ").split())
    steps = [s for s in steps if same(s) != same(first_step)] or steps  # the first step is shown above them
    allowed = {l.url for l in links}
    kept = [l for l in draft.links if l.url in allowed]
    return PrepCard(
        assignment_id=assignment.id,
        kind=kind,
        summary=summary,
        first_step=first_step,
        steps=steps,
        links=kept or links,
        watch_out=_watch_out(conn, assignment, kind, draft.watch_out, text, [d.text for d in docs if d.readable]),
        effort=effort,
        quick=quick,
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
    conn: sqlite3.Connection,
    now: datetime,
    horizon_days: int | None = None,
    limit: int | None = None,
    progress: Progress | None = None,
) -> list[PrepCard]:
    """Generate cards for upcoming assignments that don't have one yet, or whose card is stale
    (a file it couldn't open has been uploaded since), soonest due first.

    Model cards cost money, so they're capped per call; template cards are free and uncapped.
    Model cards are built CARD_WORKERS at a time, each on its own DB connection; a card that
    fails is reported in `progress` instead of stopping the rest.
    """
    progress = progress or Progress()
    have = {c.assignment_id for c in db.list_cards(conn) if not c.stale}
    todo = [
        a for a in upcoming(db.list_assignments(conn), now, horizon_days or settings.card_horizon_days)
        if a.id not in have
    ]
    if not meta_client.configured():
        return [build_prep_card(conn, a) for a in todo]
    todo = todo[: limit or settings.max_cards_per_sync]
    progress.phase("cards", total=len(todo))

    def build(a: Assignment) -> PrepCard | None:
        progress.begin(a.name)
        own = db.connect()
        try:
            card = build_prep_card(own, a)
            if card.generated_by == "fallback":
                progress.fail(a.name, card.model_error or "the model call failed, so this is a template card")
            return card
        except Exception as e:  # one broken card mustn't stop the others
            log.exception("card for %s failed", a.id)
            progress.fail(a.name, f"{type(e).__name__}: {e}")
            return None
        finally:
            own.close()
            progress.end(a.name)

    if not todo:
        return []
    with ThreadPoolExecutor(max_workers=min(CARD_WORKERS, len(todo))) as pool:
        return [c for c in pool.map(build, todo) if c is not None]
