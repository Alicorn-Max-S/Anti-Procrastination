"""Student model: where you lose marks, from graded tests, rubric rows and teacher comments.

The topic x question-type grid comes from `test_rows`; the evidence list adds rubric losses and
teacher comments from Canvas. Feeds the "watch out for" line on homework cards (and the model's
`past_feedback` / `weak_spots` tools) and picks the weakest cells for test practice sets.

Only actionable criticism counts as evidence: grade notes ("OG: 78.6%"), praise ("Very well done")
and admin notes ("you can find the rubric on the shared doc") are dropped, sentence by sentence.
A watch-out also has to fit the assignment (same kind of work, or a shared rubric criterion);
no line beats a junk line.
"""

import re
import sqlite3
from collections import defaultdict

from app import db
from app.models import Assignment


def weakness_grid(conn: sqlite3.Connection, course_id: int) -> dict[tuple[str, str], float]:
    """Map (topic, question_type) -> fraction of marks lost. Higher = weaker."""
    return {(c["topic"], c["qtype"]): c["fraction"] for c in grid_cells(conn, course_id)}


def grid_cells(conn: sqlite3.Connection, course_id: int) -> list[dict]:
    """One dict per (topic, qtype) cell with marks lost, marks possible, fraction and question count."""
    lost: dict[tuple[str, str], float] = defaultdict(float)
    possible: dict[tuple[str, str], float] = defaultdict(float)
    count: dict[tuple[str, str], int] = defaultdict(int)
    for r in db.list_test_rows(conn, course_id):
        if not r.marks_possible or r.marks is None:
            continue
        key = (r.topic or "unknown", r.qtype or "unknown")
        possible[key] += r.marks_possible
        lost[key] += max(0.0, r.marks_possible - r.marks)
        count[key] += 1
    return [
        {
            "topic": topic,
            "qtype": qtype,
            "lost": lost[(topic, qtype)],
            "possible": possible[(topic, qtype)],
            "fraction": lost[(topic, qtype)] / possible[(topic, qtype)],
            "count": count[(topic, qtype)],
        }
        for topic, qtype in sorted(possible)
    ]


def grid_table(conn: sqlite3.Connection, course_id: int) -> dict:
    """The grid shaped for the dashboard: row labels, column labels and the cells."""
    cells = grid_cells(conn, course_id)
    return {
        "topics": sorted({c["topic"] for c in cells}),
        "qtypes": sorted({c["qtype"] for c in cells}),
        "cells": cells,
    }


def weakest_cells(conn: sqlite3.Connection, course_id: int, n: int = 3) -> list[dict]:
    """The n cells where the most marks are lost, each with up to two missed questions as examples."""
    cells = [c for c in grid_cells(conn, course_id) if c["lost"] > 0]
    cells.sort(key=lambda c: (c["fraction"], c["lost"]), reverse=True)
    rows = db.list_test_rows(conn, course_id)
    out = []
    for c in cells[:n]:
        missed = [
            r for r in rows
            if (r.topic or "unknown") == c["topic"] and (r.qtype or "unknown") == c["qtype"]
            and r.marks is not None and r.marks_possible and r.marks < r.marks_possible
        ]
        out.append(
            {
                **c,
                "examples": [
                    {"question": r.question, "student_answer": r.student_answer, "teacher_note": r.teacher_note}
                    for r in missed[:2]
                ],
            }
        )
    return out


# ---- what counts as actionable ---------------------------------------------------------------

_GRADE_ONLY = re.compile(r"^\W*(?:[a-z]{1,12}\s*[:=]?\s*)?\d+(?:\.\d+)?\s*(?:%|/\s*\d+(?:\.\d+)?)?\W*$", re.I)
_ADMIN = re.compile(
    r"you can find|shared (?:google )?doc|schedule a (?:conference|meeting)|see me\b|email me|let me know|"
    r"reach out|thank you|thanks\b|feel free|is attached|posted (?:on|in)",
    re.I,
)
_CRITICISM = re.compile(
    r"\b(?:suggest\w*|next time|in the future|needs? to|need more|make sure|be sure|remember to|don'?t|do not|"
    r"forg[eo]t\w*|missing|missed|lacks?|lacking|instead of|try to|work on|should|could have|would have|"
    r"could be|would be|more (?:detail|evidence|explanation|specific|analysis|depth|precise|careful|context|"
    r"examples?)|unclear|incorrect|wrong|careful|watch (?:out|for)|double-check|check (?:your|the)|"
    r"show (?:your|all) work|cite|but|however|although|lost|deduct\w*|avoid|vague|"
    r"too (?:short|long|general|broad)|clarify|revise|proofread|add (?:a|an|more))\b",
    re.I,
)
SMALL_LOSS = 0.2  # a rubric row with no actionable comment counts only when at least this share was lost


def actionable(text: str) -> str:
    """The sentences of a teacher comment that say what to do differently; '' if there are none."""
    text = " ".join((text or "").replace("\xa0", " ").split())
    if not text or _GRADE_ONLY.match(text):
        return ""
    kept = [
        s for s in re.split(r"(?<=[.!?])\s+", text)
        if _CRITICISM.search(s) and not _ADMIN.search(s) and not _GRADE_ONLY.match(s)
    ]
    return " ".join(kept)


def feedback_evidence(
    conn: sqlite3.Connection, course_id: int, kind: str | None = None, limit: int = 12
) -> list[dict]:
    """Actionable rubric losses, teacher comments and missed test questions in a course, worst first.

    An assignment's kind is its card's kind when it has one (the title guess otherwise)."""
    card_kinds = {c.assignment_id: c.kind for c in db.list_cards(conn)}
    items: list[tuple[float, dict]] = []
    for a in db.list_assignments(conn, course_id):
        a_kind = card_kinds.get(a.id, a.kind)
        if kind and a_kind != kind:
            continue
        for r in a.rubric:
            if r.points is None or not r.points_possible or r.points >= r.points_possible:
                continue
            lost = r.points_possible - r.points
            comment = actionable(r.comment)
            if lost / r.points_possible < SMALL_LOSS and not comment:
                continue
            items.append(
                (
                    lost / r.points_possible,
                    {
                        "source": "rubric",
                        "assignment_id": a.id,
                        "assignment": a.name,
                        "kind": a_kind,
                        "criterion": r.criterion,
                        "points_lost": lost,
                        "points_possible": r.points_possible,
                        "teacher_comment": comment,
                    },
                )
            )
        for c in a.comments:
            if text := actionable(c.text):
                items.append(
                    (
                        0.5,
                        {"source": "comment", "assignment_id": a.id, "assignment": a.name, "kind": a_kind,
                         "author": c.author, "text": text},
                    )
                )
    if kind in (None, "test"):
        for r in db.list_test_rows(conn, course_id):
            if r.marks is None or not r.marks_possible or r.marks >= r.marks_possible:
                continue
            items.append(
                (
                    (r.marks_possible - r.marks) / r.marks_possible,
                    {
                        "source": "graded_test",
                        "test": r.source,
                        "topic": r.topic,
                        "qtype": r.qtype,
                        "question": r.question,
                        "student_answer": r.student_answer,
                        "teacher_note": r.teacher_note,
                    },
                )
            )
    items.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in items[:limit]]


def _words(text: str) -> str:
    """Lowercase words only, so quotes and topics match across punctuation and curly apostrophes."""
    text = text.lower().replace("’", "'").replace("‘", "'")
    return " ".join(re.findall(r"[a-z0-9']+", text))


def _topic_in(topic: str, text: str) -> bool:
    wanted = [w for w in _words(topic.replace("_", " ")).split() if len(w) > 3]
    have = set(_words(text).split())
    return bool(wanted) and all(w in have for w in wanted)


def relevant_evidence(conn: sqlite3.Connection, assignment: Assignment, kind: str, limit: int = 5) -> list[dict]:
    """Actionable evidence that fits this assignment's skills: the same kind of work, or a rubric
    criterion this assignment shares. Missed test questions count for tests, and for problem sets
    whose name or instructions mention the topic."""
    own = {_words(r.criterion) for r in assignment.rubric if r.criterion}
    text = f"{assignment.name} {assignment.description_html}"
    out = []
    for item in feedback_evidence(conn, assignment.course_id, None, limit=50):
        if item.get("assignment_id") == assignment.id:
            continue
        if item["source"] == "graded_test":
            fits = kind == "test" or (kind == "problem_set" and _topic_in(item["topic"], text))
        else:
            fits = item["kind"] == kind or (item["source"] == "rubric" and _words(item["criterion"]) in own)
        if fits:
            out.append(item)
    return out[:limit]


def _clip(text: str, n: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def watch_out_line(conn: sqlite3.Connection, assignment: Assignment, kind: str | None = None) -> str:
    """One sentence for a card from evidence that fits it, or '' when nothing does."""
    kind = kind or assignment.kind
    for item in relevant_evidence(conn, assignment, kind):
        if item["source"] == "rubric":
            line = (
                f"On {item['assignment']} you lost {item['points_lost']:g}/"
                f"{item['points_possible']:g} on “{item['criterion']}”"
            )
            if item["teacher_comment"]:
                line += f": “{_clip(item['teacher_comment'])}”"
            return line + "."
        if item["source"] == "comment":
            return f"{item['author'] or 'Your teacher'} on {item['assignment']}: “{_clip(item['text'])}”"
        if item["teacher_note"]:
            return f"On a past {item['topic'].replace('_', ' ')} question: “{_clip(item['teacher_note'])}”"
    if kind == "test":
        cells = weakest_cells(conn, assignment.course_id, n=1)
        if cells:
            c = cells[0]
            return (
                f"On graded tests you lose the most on {c['topic']} ({c['qtype'].replace('_', ' ')}): "
                f"{round(c['fraction'] * 100)}% of those marks."
            )
    return ""


def quote_checked(line: str, sources: list[str]) -> bool:
    """True when the line quotes something and every quoted passage (split at ellipses) appears in
    `sources`. Drops watch-outs that invent or misquote the teacher."""
    corpus = " | ".join(_words(s) for s in sources)
    quoted = re.findall(r"[“\"]([^”\"]+)[”\"]", line)
    pieces = [_words(p) for q in quoted for p in re.split(r"\.\.\.|…", q)]
    pieces = [p for p in pieces if p]
    return bool(pieces) and all(p in corpus for p in pieces)
