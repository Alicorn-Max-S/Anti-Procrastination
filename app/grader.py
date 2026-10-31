"""Grader: photos or scans of marked tests -> a graded test with sections, and one row per question.

One model call per page reads everything the teacher wrote: section subtotals ("26/32"), per-question
deductions ("-1"), X and check marks, circled errors, half points, a bonus, page totals and the
overall score ("80.5 + 2 = 82.5%"). Code then joins the pages and checks the arithmetic: questions
must add up to their section's subtotal, sections to each page total, and sections plus bonus to
the written score. Where a section only has X marks, its lost points are split across its wrong
questions (those rows are `estimated`). Anything that doesn't add up is listed in `mismatches`,
which flags the test for review instead of storing it as if it were right.

The course comes from the test itself: the model picks from the course list, and code falls back
to matching the title against course names and assignment titles. Question types come from a list
per course, seeded by subject and grown from section instructions, so names stay stable and the
student-model grid lines up across tests. Bonus questions are kept apart and never count as lost.

    python -m app.grader PATH [--course ID] [--fresh]   # read one file into the DB, replacing an earlier reading
"""

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app import db, materials, meta_client
from app.clock import clock
from app.config import settings
from app.models import Course, GradedTest, TestRow, TestSection

MAX_PAGES = 12  # pages read from one upload; each is one model call
EPS = 0.01

# ---- question types per course ----------------------------------------------------------------
# Types are the kind of task a section's instructions set; topics are the skill it tests. Each
# course keeps its own list (kv `grader.qtypes.<course_id>`), seeded from its subject.

LANGUAGE_QTYPES = {
    "word_bank": "choose a word from a word bank for each blank, conjugating or adapting it",
    "fill_in_blank": "fill each blank with one form: a pronoun, article, ending or conjugation",
    "conjugation": "conjugate a given verb for a given subject or tense",
    "sentence_answer": "answer questions in full sentences using a given structure or cue",
    "translation_to_target": "translate English sentences into the language being learned",
    "translation_to_english": "translate sentences from the language being learned into English",
    "question_formation": "write the question that a given answer responds to",
    "error_correction": "find and correct the errors in a passage",
    "multiple_choice": "pick one of the given options",
    "matching": "match items from two lists",
    "vocabulary": "give the word for a meaning, or the meaning of a word",
    "reading_comprehension": "answer questions about a written passage",
    "listening": "answer questions about something heard",
    "composition": "write a paragraph or more in the language being learned",
}
MATH_QTYPES = {
    "multiple_choice": "pick one of the given options",
    "computation": "compute a value, derivative, integral or limit; the answer is graded",
    "show_work": "a multi-step free-response problem where the work is graded",
    "justification": "justify or prove a claim, e.g. check a theorem's conditions",
    "setup": "write the equation, integral or model without solving it",
    "graph_or_table": "answer from a given graph or table of values",
    "estimation": "approximate a value: Riemann sums, tangent lines, error bounds",
    "word_problem": "a context problem: rates, optimization, motion, area or volume",
}
SCIENCE_QTYPES = {
    "multiple_choice": "pick one of the given options",
    "calculation": "compute a numeric answer from given values, with units",
    "derivation": "derive a symbolic expression from principles",
    "diagram": "draw or label a diagram, e.g. a free-body or ray diagram",
    "graph_reading": "read values, slopes or areas from a graph or table",
    "graph_sketch": "sketch a graph of how a quantity changes",
    "conceptual_explanation": "explain why or predict what happens, in words",
    "ranking": "rank situations by some quantity",
    "experimental_design": "design an experiment or analyze lab data and error",
}
GENERAL_QTYPES = {
    "multiple_choice": "pick one of the given options",
    "true_false": "say whether a statement is true or false",
    "fill_in_blank": "fill each blank with a word or short phrase",
    "matching": "match items from two lists",
    "short_answer": "answer in a word, phrase or sentence",
    "calculation": "compute a numeric answer",
    "free_response": "a longer written answer",
    "translation": "translate between languages",
    "diagram": "draw or label a diagram",
    "essay": "a full essay",
}
QTYPES = list(GENERAL_QTYPES)  # the old flat list, still the fallback for a course of no known subject

_FAMILIES = [
    (re.compile(r"spanish|french|latin|german|chinese|mandarin|japanese|italian|arabic|hebrew|korean|"
                r"portuguese|russian|espa[nñ]ol|fran[cç]ais|\basl\b|sign language", re.I), LANGUAGE_QTYPES),
    (re.compile(r"calculus|algebra|geometry|statistic|\bmath|precalc|trigonometry", re.I), MATH_QTYPES),
    (re.compile(r"physics|chemistry|biology|science|anatomy|environmental", re.I), SCIENCE_QTYPES),
]


def seed_qtypes(course_name: str) -> dict[str, str]:
    for pattern, qtypes in _FAMILIES:
        if pattern.search(course_name):
            return dict(qtypes)
    return dict(GENERAL_QTYPES)


def course_qtypes(conn: sqlite3.Connection, course: Course) -> dict[str, str]:
    """This course's question types, name -> description (seeded from its subject until a test is saved)."""
    raw = db.kv_get(conn, f"grader.qtypes.{course.id}")
    return json.loads(raw) if raw else seed_qtypes(course.name)


def remember_qtypes(conn: sqlite3.Connection, course: Course, used: dict[str, str]) -> None:
    """Keep the types a saved test used, so the next test in this course reuses the same names."""
    have = course_qtypes(conn, course)
    for name, description in used.items():
        if name and not have.get(name):
            have[name] = description
    db.kv_set(conn, f"grader.qtypes.{course.id}", json.dumps(have))


def _described(qtype: str) -> str:
    """A seed list's description of this type, for one picked from another subject's list."""
    for qtypes in (*(q for _, q in _FAMILIES), GENERAL_QTYPES):
        if qtype in qtypes:
            return qtypes[qtype]
    return ""


def snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


# ---- what the model reads from one page -------------------------------------------------------


class PageItem(BaseModel):
    number: str
    question: str
    student_answer: str
    blanks: int
    marks_seen: str
    result: Literal["correct", "incorrect", "partial", "unmarked"]
    wrong_marks: int
    points_lost: float | None
    points_earned: float | None
    points_possible: float | None
    teacher_note: str
    topic: str


class PageSection(BaseModel):
    name: str
    instructions: str
    continues_previous: bool
    bonus: bool
    qtype: str
    new_qtype_description: str
    topic: str
    subtotal_earned: float | None
    subtotal_possible: float | None
    items: list[PageItem]


class GradedPage(BaseModel):
    course_id: int
    subject: str
    title: str
    sections: list[PageSection]
    page_total: float | None
    score_written: str
    score: float | None
    score_possible: float | None
    bonus_added: float | None


INSTRUCTIONS = """You read one page of a test or quiz that a teacher has marked by hand, and report
exactly what is on it. Read every kind of mark: X marks (XX counts as two), check marks, circled
words, crossed-out words with corrections written above, deductions such as "-1" or "-2", half
points such as "0.5" or "-½", "O.K.", a subtotal written beside a section ("26/32", or 26 over 32:
earned over possible), a total for the whole page (often a lone number in a corner), bonus points
(often circled next to a BONUS section) and the overall score (e.g. "80.5 + 2 = 82.5%"). Never
invent a number that isn't on the page. Copy the student's answers as written, misspellings
included. Don't copy anyone's name into any field."""


def _types_text(qtypes: dict[str, str]) -> str:
    return "\n".join(f"- {n}: {d}" if d else f"- {n}" for n, d in qtypes.items())


def _page_prompt(
    page: int,
    pages: int,
    courses: list[Course],
    lists: dict[int, dict[str, str]],
    course: Course | None,
    topics: list[str],
    previous: "PageSection | None",
) -> str:
    if course:
        where = (
            f"This test is from the course {course.name}. Still set course_id to the course the page "
            f"itself points to, from this list (0 if you can't tell): "
            + "; ".join(f"{c.id} {c.name}" for c in courses)
            + f".\n\nqtype must be one of these types for {course.name}:\n{_types_text(lists[course.id])}"
        )
    else:
        groups: dict[tuple, list[Course]] = {}
        for c in courses:
            groups.setdefault(tuple(lists[c.id]), []).append(c)
        blocks = []
        for n, (names, members) in enumerate(groups.items(), 1):
            blocks.append(
                f"Types list {n}, for " + "; ".join(f"{c.id} {c.name}" for c in members) + ":\n"
                + _types_text(lists[members[0].id])
            )
        where = (
            "course_id: the course this test is from, judged from its language, title and content. "
            "Pick from the courses named below; 0 if none fits.\n\nqtype must come from the types list "
            "of that course:\n\n" + "\n\n".join(blocks)
        )
    carry = ""
    if previous is not None:
        last = previous.items[-1].number if previous.items else ""
        carry = (
            f'\nThe previous page ended in the section "{previous.name}" ({previous.instructions[:120]})'
            f"{f', at question {last}' if last else ''}. If this page carries on with it before any new "
            "instructions, put those questions in a first section with continues_previous=true.\n"
        )
    return f"""This is page {page} of {pages} of one graded test. Report every section on this page, in order.
{carry}
Sections: one per set of instructions. name: 2-5 English words for what the section asks
(e.g. "Verb from word bank"). instructions: the printed instructions, first sentence. bonus: true
for a BONUS / extra-credit section. subtotal_earned / subtotal_possible: the section score the
teacher wrote ("26/32" -> 26 and 32), null if none is written on this page.

Items: one per numbered question, in order. For a "find/correct the errors" passage, one item
per error: the phrase with the error as question, the student's correction as student_answer
("" if the student missed it). question: as printed, blanks as ____. blanks: answer slots in the
item (2 if it has two blanks). marks_seen: every teacher mark at the item, verbatim ("X", "XX",
"✓", "-1", "0.5", "circled r", "O.K."), "" if none. result: incorrect if any X, cross, circled
error or deduction is at it; partial if only part of it is wrong or it lost only some points;
correct if it is checked or marked O.K.; an error the student missed, or a question left blank, is
incorrect; unmarked if the teacher left no mark on an answer. wrong_marks: how
many X / wrong marks are at it (XX = 2). points_lost: points taken off at the item ("-2" -> 2; in a
section marked by deductions, a bare "0.5" beside an item is 0.5 taken off). points_earned: only
if the teacher wrote what the item earned ("3/4" -> 3). points_possible: only if printed or written
for the item. teacher_note: the teacher's words and corrections at the item, not the marks
("se las", "Para (algo masc. plural)"), "" if none. topic: "" unless it differs from the section's.

page_total: a total the teacher wrote for the whole page, null if none. score_written: the overall
score exactly as written ("80.5 + 2 = 82.5%"), "" if it isn't on this page; score: its final number
(82.5); score_possible: what it is out of (100 for a percent); bonus_added: bonus points added in it
(2 in "80.5 + 2"), null if none.

{where}
If no type fits a section, make a new short snake_case qtype and describe it in
new_qtype_description; otherwise new_qtype_description is "".
topic: the skill the section tests, short snake_case (e.g. direct_object_pronouns).
{"Reuse one of these topics when it fits: " + ", ".join(topics) if topics else ""}
subject: the subject the test is in (e.g. "Spanish"); title: the test's printed title, "" if none."""


def split_pages(path: Path, folder: Path) -> list[Path]:
    """One file per page: a PDF's pages copied out one by one (up to MAX_PAGES); an image as is."""
    if path.suffix.lower() != ".pdf":
        return [path]
    import pypdf

    reader = pypdf.PdfReader(str(path))
    if len(reader.pages) <= 1:
        return [path]
    out = []
    for i, page in enumerate(reader.pages[:MAX_PAGES], 1):
        writer = pypdf.PdfWriter()
        writer.add_page(page)
        part = folder / f"page{i}.pdf"
        with part.open("wb") as f:
            writer.write(f)
        out.append(part)
    return out


def read_pages(conn: sqlite3.Connection, path: Path, course_id: int | None = None) -> list[GradedPage]:
    """Read each page with one model call, in order. Pages after the first get the course the
    first page pointed to, so their question types come from that course's list."""
    courses = db.list_courses(conn)
    lists = {c.id: course_qtypes(conn, c) for c in courses}
    by_id = {c.id: c for c in courses}
    course = by_id.get(course_id or 0)
    out: list[GradedPage] = []
    with tempfile.TemporaryDirectory() as tmp:
        files = split_pages(path, Path(tmp))
        for i, f in enumerate(files, 1):
            topics = sorted({r.topic for r in db.list_test_rows(conn, course.id) if r.topic}) if course else []
            previous = out[-1].sections[-1] if out and out[-1].sections else None
            prompt = _page_prompt(i, len(files), courses, lists, course, topics, previous)
            page = meta_client.generate_structured(prompt, GradedPage, files=[f], instructions=INSTRUCTIONS)
            out.append(page)
            if course is None:
                course = by_id.get(detect_course(conn, out, courses))
    return out


def detect_course(conn: sqlite3.Connection, pages: list[GradedPage], courses: list[Course]) -> int:
    """The course most pages pointed to; else the best match of title and subject against course
    names and their assignments' names; 0 if nothing matches."""
    valid = {c.id for c in courses}
    votes = Counter(p.course_id for p in pages if p.course_id in valid)
    if votes:
        return votes.most_common(1)[0][0]
    words = materials.stems(" ".join(f"{p.title} {p.subject}" for p in pages))
    best, best_score = 0, 0
    for c in courses:
        score = 3 * len(words & materials.stems(c.name))
        score += max((len(words & materials.stems(a.name)) for a in db.list_assignments(conn, c.id)), default=0)
        if score > best_score:
            best, best_score = c.id, score
    return best


# ---- putting the pages together and checking the arithmetic -----------------------------------


@dataclass
class _Section:
    head: PageSection
    page: int
    items: list[PageItem] = field(default_factory=list)
    earned: float | None = None
    possible: float | None = None
    total_page: int = 0  # the page its subtotal is written on


def _join(pages: list[GradedPage]) -> list[_Section]:
    out: list[_Section] = []
    for n, page in enumerate(pages, 1):
        for s in page.sections:
            if s.continues_previous and out:
                sec = out[-1]
            else:
                sec = _Section(head=s, page=n)
                out.append(sec)
            sec.items.extend(s.items)
            if s.subtotal_earned is not None or s.subtotal_possible is not None:
                sec.earned = s.subtotal_earned if s.subtotal_earned is not None else sec.earned
                sec.possible = s.subtotal_possible if s.subtotal_possible is not None else sec.possible
                sec.total_page = n
    return out


def _spread(total: float, weights: list[float], caps: list[float]) -> tuple[list[float], float]:
    """Split `total` in proportion to `weights`, never giving a share more than its cap. Returns the
    shares and whatever couldn't be placed."""
    out = [0.0] * len(weights)
    open_ = [i for i, w in enumerate(weights) if w > 0 and caps[i] > EPS]
    left = total
    while open_ and left > EPS:
        wsum = sum(weights[i] for i in open_)
        full = [i for i in open_ if out[i] + left * weights[i] / wsum > caps[i] + 1e-9]
        if not full:
            for i in open_:
                out[i] += left * weights[i] / wsum
            left = 0.0
            break
        for i in full:
            left -= caps[i] - out[i]
            out[i] = caps[i]
        open_ = [i for i in open_ if i not in full]
    return out, max(0.0, left)


def _g(x: float) -> str:
    return f"{round(x, 2):g}"


@dataclass
class _Fit:
    caps: list[float | None]
    losses: list[float | None]
    estimated: list[bool]
    earned: float | None
    possible: float | None
    written: bool
    mismatch: str = ""
    note: str = ""


def _known_loss(it: PageItem, cap: float | None, flip: bool = False) -> float | None:
    if it.points_lost is not None:
        return abs(it.points_lost)
    if it.points_earned is not None:
        if flip and it.points_possible is None:  # a bare number in a deductions section
            return abs(it.points_earned)
        if (it.points_possible or cap) is not None:
            return max(0.0, (it.points_possible or cap) - it.points_earned)
    return None


def _wrong(it: PageItem, loss: float | None) -> bool:
    return it.result in ("incorrect", "partial") or (loss or 0) > EPS


def _weight(it: PageItem, loss: float | None) -> float:
    if loss:
        return loss
    return max(it.wrong_marks, 1) * (0.5 if it.result == "partial" and not it.wrong_marks else 1)


def fit_section(sec: _Section, name: str) -> _Fit:
    """Per-question marks that agree with the section's subtotal, and what didn't add up."""
    items = sec.items
    n = len(items)
    blanks = [max(1, it.blanks) for it in items]
    written_pp = [it.points_possible for it in items]
    P, E = sec.possible, sec.earned
    if P is None and n and all(p is not None for p in written_pp):
        P = sum(written_pp)
    if P is None:
        # Nothing says what the section is worth: one point per answer, from the marks alone.
        caps = [float(b) for b in blanks]
        losses = []
        for it, cap in zip(items, caps):
            if it.result == "incorrect":
                losses.append(min(cap, float(max(it.wrong_marks, 1))) if it.blanks > 1 else cap)
            elif it.result == "partial":
                losses.append(cap / 2)
            else:
                losses.append(0.0)
        earned = sum(caps) - sum(losses)
        return _Fit(caps, losses, [True] * n, earned, sum(caps), False,
                    note=f"{name}: no points written; counted one point per answer")
    if n and all(p is not None for p in written_pp) and abs(sum(written_pp) - P) < EPS:
        caps = [float(p) for p in written_pp]
    else:
        caps = [P * b / sum(blanks) for b in blanks] if n else []
    known = [_known_loss(it, c) for it, c in zip(items, caps)]
    if E is None:
        losses, est = [], []
        for it, c, d in zip(items, caps, known):
            if d is not None:
                losses.append(min(d, c))
                est.append(False)
            elif _wrong(it, None):
                losses.append(c if it.result == "incorrect" else c / 2)
                est.append(True)
            else:
                losses.append(0.0)
                est.append(False)
        note = f"{name}: no subtotal written; added up from its questions" if any(est) else ""
        return _Fit(caps, losses, est, P - sum(losses), P, False, note=note)

    L = P - E
    if L < -EPS:
        return _Fit(caps, [0.0] * n, [False] * n, E, P, True,
                    mismatch=f"{name}: {_g(E)}/{_g(P)} is more than the section is worth")
    flipped = [_known_loss(it, c, flip=True) for it, c in zip(items, caps)]
    if abs(sum(d or 0 for d in known) - L) > EPS and abs(sum(d or 0 for d in flipped) - L) <= EPS:
        known = flipped  # "0.5" beside a question meant 0.5 off, not 0.5 earned
    K = sum(d or 0 for d in known)
    unknown = [i for i, (it, d) in enumerate(zip(items, known)) if d is None and _wrong(it, None)]
    if not unknown and L - K > EPS:
        # what's left was lost on blanks the teacher left unmarked: a missed error, a skipped question
        unknown = [i for i, (it, d) in enumerate(zip(items, known))
                   if d is None and it.result == "unmarked" and not it.student_answer.strip()]
    losses = [d if d is not None else 0.0 for d in known]
    est = [False] * n
    mismatch = ""
    if unknown and L - K > EPS:
        shares, left = _spread(L - K, [_weight(items[i], None) for i in unknown], [caps[i] for i in unknown])
        for i, s in zip(unknown, shares):
            losses[i], est[i] = s, True
        if left > EPS:
            mismatch = f"{name}: {_g(L)} points off, more than its wrong questions are worth"
    elif not unknown and abs(K - L) <= EPS:
        pass
    else:
        wrong = [i for i, (it, d) in enumerate(zip(items, known)) if _wrong(it, d)]
        if L > EPS and not wrong:
            mismatch = f"{name}: {_g(L)} points off ({_g(E)}/{_g(P)}), but no question is marked wrong"
            wrong = list(range(n))
        elif unknown:
            mismatch = (f"{name}: {len(unknown)} more question{'s are' if len(unknown) > 1 else ' is'} marked "
                        f"wrong after the {_g(K)} points already taken off, but the section shows only "
                        f"{_g(L)} off ({_g(E)}/{_g(P)})")
        else:
            mismatch = (f"{name}: the marks at its questions take off {_g(K)}, but the section shows "
                        f"{_g(E)}/{_g(P)} ({_g(L)} off)")
        shares, _ = _spread(L, [_weight(items[i], known[i]) for i in wrong], [caps[i] for i in wrong])
        losses = [0.0] * n
        for i, s in zip(wrong, shares):
            losses[i], est[i] = s, True
    return _Fit(caps, losses, est, E, P, True, mismatch=mismatch)


def reconcile(
    pages: list[GradedPage], course_id: int, detected: int, source: str, qtypes: dict[str, str]
) -> tuple[GradedTest, list[TestRow], dict[str, str]]:
    """The test, its rows (unsaved) and the question types it used, name -> description."""
    sections = _join(pages)
    used: dict[str, str] = {}
    rows: list[TestRow] = []
    results: list[TestSection] = []
    mismatches: list[str] = []
    notes: list[str] = []
    bonus_points = 0.0
    bonus_written = False
    seen_names: Counter = Counter()
    for sec in sections:
        h = sec.head
        qtype = snake(h.qtype) or "other"
        used[qtype] = qtypes.get(qtype) or h.new_qtype_description.strip() or _described(qtype)
        name = h.name.strip() or qtype.replace("_", " ").capitalize()
        seen_names[name] += 1
        if seen_names[name] > 1:
            name = f"{name} ({seen_names[name]})"
        topic = snake(h.topic)
        wrong = 0
        if h.bonus:
            if sec.earned is not None:
                bonus_points += sec.earned
                bonus_written = True
            for it in sec.items:
                ok = {"correct": True, "incorrect": False, "partial": False}.get(it.result)
                wrong += ok is False
                rows.append(_row(it, course_id, source, qtype, topic, name, None, None, ok, bonus=True))
            results.append(TestSection(name=name, instructions=h.instructions, qtype=qtype, topic=topic,
                                       earned=sec.earned, possible=sec.possible, written=sec.earned is not None,
                                       bonus=True, questions=len(sec.items), wrong=wrong, page=sec.page))
            continue
        fit = fit_section(sec, name)
        if fit.mismatch:
            mismatches.append(fit.mismatch)
        if fit.note:
            notes.append(fit.note)
        for it, cap, loss, est in zip(sec.items, fit.caps, fit.losses, fit.estimated):
            ok = loss < EPS if loss is not None else {"correct": True, "incorrect": False}.get(it.result)
            wrong += ok is False
            # unrounded, so a section's rows add up to its subtotal (the page rounds for display)
            marks = max(0.0, cap - loss) if cap is not None and loss is not None else None
            rows.append(_row(it, course_id, source, qtype, topic, name, marks, cap, ok, estimated=est))
        results.append(TestSection(name=name, instructions=h.instructions, qtype=qtype, topic=topic,
                                   earned=fit.earned, possible=fit.possible, written=fit.written,
                                   questions=len(sec.items), wrong=wrong, page=sec.total_page or sec.page))

    # Page totals: the sections whose subtotal is written on a page add up to its total.
    for n, page in enumerate(pages, 1):
        if page.page_total is None:
            continue
        here = [r for r, s in zip(results, sections) if (s.total_page or s.page) == n]
        on_page = [r for r in here if not r.bonus]
        if not on_page or not all(r.written for r in on_page):
            notes.append(f"Page {n}: its total can't be checked: not every section on it has a subtotal")
            continue
        got = sum(r.earned for r in on_page)
        with_bonus = got + sum(r.earned or 0 for r in here if r.bonus)
        if abs(got - page.page_total) > EPS and abs(with_bonus - page.page_total) > EPS:
            mismatches.append(f"Page {n}: its sections add up to {_g(got)}, but the page total says {_g(page.page_total)}")

    scored = [p for p in pages if p.score is not None]
    head = next((p for p in scored if p.score_written.strip()), scored[0] if scored else None)
    added = next((p.bonus_added for p in pages if p.bonus_added is not None), None)
    if added is not None and bonus_written and abs(added - bonus_points) > EPS:
        mismatches.append(f"Bonus: the bonus section shows {_g(bonus_points)}, but the score adds {_g(added)}")
    bonus = added if added is not None else bonus_points
    main = [r for r in results if not r.bonus]
    earned = sum(r.earned for r in main) if main and all(r.earned is not None for r in main) else None
    possible = sum(r.possible for r in main) if main and all(r.possible is not None for r in main) else None
    score = head.score if head else None
    score_written = head.score_written.strip() if head else ""
    score_possible = head.score_possible if head else None
    if score is not None and score_possible is None:
        score_possible = 100.0 if "%" in score_written else possible
    if score is None and earned is not None:
        score, score_possible = earned + bonus, possible
    elif score is not None and earned is not None and possible:
        if not all(r.written for r in main):
            notes.append("The written score can't be checked: not every section has a subtotal")
        else:
            total = earned + bonus
            if score_possible and abs(score_possible - possible) <= EPS:
                ok = abs(total - score) <= EPS
            elif score_possible:
                ok = abs(total / possible * score_possible - score) <= 0.51
            else:
                ok = abs(total - score) <= EPS
            if not ok:
                extra = f" + {_g(bonus)} bonus" if bonus else ""
                mismatches.append(f"The sections add up to {_g(earned)}/{_g(possible)}{extra}, but the score "
                                  f"written on the test is {score_written or _g(score)}")
    title = next((p.title.strip() for p in pages if p.title.strip()), "")
    subject = next((p.subject.strip() for p in pages if p.subject.strip()), "")
    test = GradedTest(
        course_id=course_id, detected_course_id=detected, source=source, title=title, subject=subject,
        pages=len(pages), score=score, score_possible=score_possible, score_written=score_written,
        earned=earned, possible=possible, bonus=bonus, sections=results, mismatches=mismatches, notes=notes,
        created_at=clock.real_now(),
    )
    return test, rows, used


def _row(it: PageItem, course_id: int, source: str, qtype: str, topic: str, section: str,
         marks: float | None, possible: float | None, ok: bool | None, bonus: bool = False,
         estimated: bool = False) -> TestRow:
    question = " ".join(it.question.split())
    return TestRow(
        course_id=course_id, source=source,
        question=f"{it.number}. {question}" if it.number and not question.startswith(f"{it.number}.") else question,
        student_answer=it.student_answer, marks=marks, marks_possible=possible,
        teacher_note=it.teacher_note.strip(), topic=snake(it.topic) or topic, qtype=qtype, section=section,
        correct=ok, bonus=bonus, estimated=estimated,
    )


# ---- reading and saving a test ----------------------------------------------------------------


def _readings(sha: str) -> Path:
    return Path(settings.files_dir) / "tests" / "readings" / f"{sha}.json"


def read_test(
    conn: sqlite3.Connection, path: Path, course_id: int | None = None, fresh: bool = False
) -> tuple[GradedTest, list[TestRow], dict[str, str]]:
    """Read a graded test file and check it. `course_id` files it there; None files it under the
    course the test points to (0 when it can't tell). Nothing is saved but the pages as read,
    cached by file hash: the same file again is re-checked with no model calls unless `fresh`."""
    cache = _readings(file_sha(path))
    if cache.exists() and not fresh:
        pages = [GradedPage.model_validate(p) for p in json.loads(cache.read_text(encoding="utf-8"))]
    else:
        pages = read_pages(conn, path, course_id)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([p.model_dump() for p in pages], ensure_ascii=False), encoding="utf-8")
    courses = db.list_courses(conn)
    detected = detect_course(conn, pages, courses)
    target = course_id if course_id is not None else detected
    course = next((c for c in courses if c.id == target), None)
    qtypes = course_qtypes(conn, course) if course else {}
    test, rows, used = reconcile(pages, target, detected, path.name, qtypes)
    return test.model_copy(update={"sha": file_sha(path)}), rows, used


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(conn: sqlite3.Connection, test: GradedTest, rows: list[TestRow], used: dict[str, str]) -> GradedTest:
    """Store a test and its rows, replacing an earlier reading of the same file (same content or
    same saved name), and keep the types it used in its course's list."""
    for old in db.list_graded_tests(conn):
        if (test.sha and old.sha == test.sha) or old.source == test.source:
            db.delete_graded_test(conn, old.id)
    conn.execute("DELETE FROM test_rows WHERE source = ? AND test_id IS NULL", (test.source,))
    test_id = db.save_graded_test(conn, test)
    test = test.model_copy(update={"id": test_id})
    db.save_test_rows(conn, [r.model_copy(update={"test_id": test_id, "course_id": test.course_id}) for r in rows])
    course = next((c for c in db.list_courses(conn) if c.id == test.course_id), None)
    if course:
        remember_qtypes(conn, course, used)
    return test


def move(conn: sqlite3.Connection, test_id: int, course_id: int) -> GradedTest | None:
    """File a test under another course; its question types join that course's list."""
    test = db.move_graded_test(conn, test_id, course_id)
    course = next((c for c in db.list_courses(conn) if c.id == course_id), None)
    if test and course:
        old = {r.qtype for r in db.list_test_rows(conn, course_id) if r.test_id == test_id and r.qtype}
        known = {}
        for c in db.list_courses(conn):
            known.update({k: v for k, v in course_qtypes(conn, c).items() if k in old})
        remember_qtypes(conn, course, {q: known.get(q, "") for q in old})
    return test


def summary(test: GradedTest, course_name: str = "") -> str:
    """One plain line: score, bonus, and whether it needs a look."""
    parts = []
    if test.score is not None:
        out_of = "%" if test.score_possible == 100 else f"/{_g(test.score_possible or 0)}"
        parts.append(f"{_g(test.score)}{out_of}")
    if test.earned is not None and test.possible:
        parts.append(f"({_g(test.earned)}/{_g(test.possible)}" + (f" + {_g(test.bonus)} bonus)" if test.bonus else ")"))
    if course_name:
        parts.append(f"in {course_name}")
    if test.needs_review:
        parts.append(f"- needs review: {len(test.mismatches)} thing{'s' if len(test.mismatches) > 1 else ''} don't add up")
    return " ".join(parts)


def main() -> None:
    p = argparse.ArgumentParser(description="Read a graded test into the DB (replaces an earlier reading of it).")
    p.add_argument("path", type=Path)
    p.add_argument("--course", type=int, default=None, help="file it under this course id instead of detecting")
    p.add_argument("--fresh", action="store_true", help="read the pages again even if this file was read before")
    args = p.parse_args()
    conn = db.connect()
    db.init_db(conn)
    before = dict(meta_client.usage)
    test, rows, used = read_test(conn, args.path, args.course, fresh=args.fresh)
    test = save(conn, test, rows, used)
    names = {c.id: c.name for c in db.list_courses(conn)}
    print(f"{settings.db_path}: test {test.id}, {len(rows)} questions, {summary(test, names.get(test.course_id, 'no course'))}")
    for s in test.sections:
        score = f"{_g(s.earned)}/{_g(s.possible)}" if s.earned is not None and s.possible is not None else (
            f"+{_g(s.earned)}" if s.bonus and s.earned is not None else "?")
        print(f"  {s.name}: {score}{' (bonus)' if s.bonus else ''}{'' if s.written or s.bonus else ' (added up)'}")
    for m in test.mismatches:
        print(f"  MISMATCH {m}")
    for n in test.notes:
        print(f"  note: {n}")
    tin = meta_client.usage["input_tokens"] - before["input_tokens"]
    tout = meta_client.usage["output_tokens"] - before["output_tokens"]
    calls = meta_client.usage["calls"] - before["calls"]
    print(f"model: {calls} calls, {tin} in + {tout} out tokens, about ${tin * 1.25e-6 + tout * 4.25e-6:.3f}")


if __name__ == "__main__":
    main()
