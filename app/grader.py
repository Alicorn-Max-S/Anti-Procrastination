"""Grader extraction: photos of graded tests -> per-question rows in `test_rows`.

Each row: question, student answer, marks, teacher note, topic, question type. Question types
come from a fixed list so the student-model grid lines up across tests, and topics already used
in the course are passed back in so the model reuses the same names. Vision via meta_client.
"""

import sqlite3
from pathlib import Path

from pydantic import BaseModel

from app import db, meta_client
from app.models import TestRow

QTYPES = [
    "multiple_choice",
    "true_false",
    "fill_in_blank",
    "matching",
    "short_answer",
    "calculation",
    "free_response",
    "translation",
    "diagram",
    "essay",
]

INSTRUCTIONS = """You read photos of graded quizzes and tests that a teacher has marked by hand.
Extract every question, in order, exactly as written. Read the student's answer and the teacher's
marks and comments carefully; if marks aren't visible for a question, use null. Never guess a
score that isn't on the page. Don't copy anyone's name into any field."""


class ExtractedQuestion(BaseModel):
    number: str
    question: str
    student_answer: str
    marks: float | None
    marks_possible: float | None
    teacher_note: str
    topic: str
    qtype: str


class ExtractedTest(BaseModel):
    questions: list[ExtractedQuestion]


def extract_rows(
    photo: Path, course_id: int, conn: sqlite3.Connection | None = None, qtypes: list[str] | None = None
) -> list[TestRow]:
    """Read one graded-test photo (or PDF scan) and return one TestRow per question (not saved)."""
    qtypes = qtypes or QTYPES
    topics = sorted({r.topic for r in db.list_test_rows(conn, course_id) if r.topic}) if conn else []
    prompt = f"""Extract each question on this graded test.

qtype must be one of: {", ".join(qtypes)}.
topic: a short snake_case name for the skill being tested (e.g. preterite_vs_imperfect).
{"Reuse one of these existing topics when it fits: " + ", ".join(topics) if topics else ""}
teacher_note: the teacher's written comment next to this question, or "" if none."""
    result = meta_client.generate_structured(prompt, ExtractedTest, files=[photo], instructions=INSTRUCTIONS)
    return [
        TestRow(
            course_id=course_id,
            source=photo.name,
            question=f"{q.number}. {q.question}" if q.number else q.question,
            student_answer=q.student_answer,
            marks=q.marks,
            marks_possible=q.marks_possible,
            teacher_note=q.teacher_note,
            topic=q.topic.strip().lower().replace(" ", "_"),
            qtype=q.qtype if q.qtype in qtypes else "other",
        )
        for q in result.questions
    ]
