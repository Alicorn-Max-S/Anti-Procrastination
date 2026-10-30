"""Student model: where you lose marks, from graded tests, rubric rows and teacher comments.

The topic x question-type grid comes from `test_rows`; the evidence list adds rubric losses and
teacher comments from Canvas. Feeds the "watch out for" line on homework cards (and the model's
`past_feedback` / `weak_spots` tools) and picks the weakest cells for test practice sets.
"""

import sqlite3
from collections import defaultdict

from app import db


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


def feedback_evidence(
    conn: sqlite3.Connection, course_id: int, kind: str | None = None, limit: int = 12
) -> list[dict]:
    """Rubric losses, teacher comments and missed test questions in a course, worst first."""
    items: list[tuple[float, dict]] = []
    for a in db.list_assignments(conn, course_id):
        if kind and a.kind != kind:
            continue
        for r in a.rubric:
            if r.points is not None and r.points_possible and r.points < r.points_possible:
                lost = r.points_possible - r.points
                items.append(
                    (
                        lost / r.points_possible,
                        {
                            "source": "rubric",
                            "assignment": a.name,
                            "kind": a.kind,
                            "criterion": r.criterion,
                            "points_lost": lost,
                            "points_possible": r.points_possible,
                            "teacher_comment": r.comment,
                        },
                    )
                )
        for c in a.comments:
            items.append(
                (0.5, {"source": "comment", "assignment": a.name, "kind": a.kind, "author": c.author, "text": c.text})
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


def _clip(text: str, n: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def watch_out_line(conn: sqlite3.Connection, course_id: int, kind: str | None = None) -> str:
    """One sentence for a homework card, grounded in comments/rubric/test rows. '' if no evidence.

    Prefers evidence from the same kind of assignment, then anything in the course.
    """
    for k in ([kind, None] if kind else [None]):
        for item in feedback_evidence(conn, course_id, k, limit=5):
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
    cells = weakest_cells(conn, course_id, n=1)
    if cells:
        c = cells[0]
        return (
            f"On graded tests you lose the most on {c['topic']} ({c['qtype'].replace('_', ' ')}): "
            f"{round(c['fraction'] * 100)}% of those marks."
        )
    return ""
