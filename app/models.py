"""Typed data shapes shared by every module (Canvas data, prep cards, schedule blocks, pings, test rows).

This is the contract between modules: Canvas code produces these, the DB stores them, the API
returns them. Add fields with defaults so rows already in the DB still load.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AssignmentKind = Literal["lab", "reading", "problem_set", "essay", "test", "other"]
ASSIGNMENT_KINDS: tuple[str, ...] = AssignmentKind.__args__
Effort = Literal["light", "medium", "heavy"]
BlockStatus = Literal["planned", "pinged", "started", "missed", "done"]
CheckinAnswer = Literal["start_now", "plus_15", "pick_time", "keep", "none"]  # keep: OK the new time after a miss


# ---- Canvas data -----------------------------------------------------------------------------


class Course(BaseModel):
    id: int
    name: str


class SubmissionComment(BaseModel):
    id: int
    assignment_id: int
    author: str
    text: str
    created_at: datetime | None = None


class RubricScore(BaseModel):
    """One rubric criterion. Filled from the assignment's rubric; `points` stays None until graded."""

    assignment_id: int
    criterion_id: str = ""
    criterion: str
    points: float | None = None
    points_possible: float | None = None
    comment: str = ""


class Assignment(BaseModel):
    id: int
    course_id: int
    name: str
    description_html: str = ""
    due_at: datetime | None = None
    points_possible: float | None = None
    html_url: str = ""
    kind: AssignmentKind = "other"
    submission_types: list[str] = Field(default_factory=list)
    external_tool_url: str = ""  # e.g. Google Assignments' LTI launch URL
    attachment_urls: list[str] = Field(default_factory=list)  # Canvas file API endpoints
    submitted: bool = False
    score: float | None = None
    graded_at: datetime | None = None
    comments: list[SubmissionComment] = Field(default_factory=list)
    rubric: list[RubricScore] = Field(default_factory=list)


# ---- Prep cards -------------------------------------------------------------------------------


class Link(BaseModel):
    label: str
    url: str


class PracticeQuestion(BaseModel):
    topic: str
    qtype: str
    prompt: str
    choices: list[str] = Field(default_factory=list)  # empty unless multiple choice
    answer: str
    explanation: str = ""
    verified: bool = False  # True when an independent second solve agreed with `answer`
    source: str = ""  # the unit document it's based on, e.g. "Presentation: Vectors (Part 1), p. 4"
    source_url: str = ""


class SourceFile(BaseModel):
    """A file or linked doc the card tried to read, shown on the card so the student knows what it saw."""

    name: str
    url: str = ""
    read: bool = True
    reason: str = ""  # why it couldn't be opened, e.g. "shared only inside the school"
    pages: int | None = None  # real page count (book pages, not PDF pages) when known


class ReadingSitting(BaseModel):
    """One sitting of a reading: a run of whole sections, by printed page numbers."""

    first_page: str
    last_page: str
    sections: list[str]
    words: int
    minutes: int
    pdf_pages: str = ""  # the same pages as the file numbers them, e.g. "1-6", when they differ
    openings: list[str] = Field(default_factory=list)  # first words of each section, for the prompt


class ReadingPlan(BaseModel):
    """How long a reading really is and where to stop, worked out from the file's text."""

    file: str
    first_page: str
    last_page: str
    pages: int
    words: int
    minutes: int
    sittings: list[ReadingSitting] = Field(default_factory=list)


class PrepCard(BaseModel):
    assignment_id: int
    kind: AssignmentKind
    summary: str
    first_step: str  # the concrete two-minute starting action
    steps: list[str]
    links: list[Link] = Field(default_factory=list)
    watch_out: str = ""  # from the student model; empty until there is evidence
    effort: Effort = "medium"
    practice: list[PracticeQuestion] = Field(default_factory=list)  # test cards only
    focus: str = ""  # test cards: which weak spots the practice set targets
    generated_by: Literal["model", "fallback", "demo"] = "model"
    sources: list[SourceFile] = Field(default_factory=list)  # what the card was built from
    instructions_seen: bool = True  # False when there was no description and no readable file
    reading: ReadingPlan | None = None  # readings with a readable file
    model_error: str = ""  # why the model card failed, when this is the template card instead
    stale: bool = False  # a file it couldn't read has arrived since (browser_files); the next sync rebuilds it


# ---- Schedule / loop --------------------------------------------------------------------------


class Block(BaseModel):
    """A planned work session for one assignment."""

    id: int | None = None
    assignment_id: int
    start: datetime
    end: datetime
    status: BlockStatus = "planned"
    at_risk: bool = False  # True when the block ends after the assignment is due


class Ping(BaseModel):
    """One notification and what the student did about it (this becomes the real usage data)."""

    id: int | None = None
    block_id: int
    sent_at: datetime
    answer: CheckinAnswer = "none"
    picked_time: datetime | None = None
    started_at: datetime | None = None


# ---- Graded tests -----------------------------------------------------------------------------


class TestRow(BaseModel):
    """One question from a graded test photo (filled by grader.py)."""

    __test__ = False  # keep pytest from collecting this as a test class

    id: int | None = None
    course_id: int
    source: str  # photo filename
    question: str = ""
    student_answer: str = ""
    marks: float | None = None
    marks_possible: float | None = None
    teacher_note: str = ""
    topic: str = ""
    qtype: str = ""
    test_id: int | None = None  # the GradedTest it came from (rows saved before tests had records: None)
    section: str = ""  # that test's section name
    correct: bool | None = None  # None when the teacher's marks don't say
    bonus: bool = False  # bonus questions never count as lost marks (marks stay None)
    estimated: bool = False  # marks split from the section subtotal, not written at the question


class TestSection(BaseModel):
    """One section of a graded test: its instructions, question type and the teacher's subtotal."""

    __test__ = False

    name: str
    instructions: str = ""
    qtype: str = ""
    topic: str = ""
    earned: float | None = None
    possible: float | None = None
    written: bool = False  # the teacher wrote this subtotal (else it is added up from the questions)
    bonus: bool = False
    questions: int = 0
    wrong: int = 0
    page: int = 1


class GradedTest(BaseModel):
    """A graded test as a whole: score as written, sections, bonus, and whether it adds up."""

    __test__ = False

    id: int | None = None
    course_id: int  # 0 until it is filed under a course
    detected_course_id: int = 0  # the course the test itself points to; 0 when it couldn't tell
    source: str  # saved filename
    sha: str = ""  # of the uploaded file, so a re-upload replaces the old reading
    title: str = ""
    subject: str = ""
    pages: int = 1
    score: float | None = None  # final score as written, bonus included (82.5)
    score_possible: float | None = None  # what the score is out of (100 for a percent)
    score_written: str = ""  # e.g. "80.5 + 2 = 82.5%"
    earned: float | None = None  # sections added up, without bonus
    possible: float | None = None
    bonus: float = 0.0
    sections: list[TestSection] = Field(default_factory=list)
    mismatches: list[str] = Field(default_factory=list)  # anything that didn't add up: needs review
    notes: list[str] = Field(default_factory=list)  # what couldn't be checked
    created_at: datetime | None = None

    @property
    def needs_review(self) -> bool:
        return bool(self.mismatches)

    @property
    def percent(self) -> float | None:
        if self.score is None or not self.score_possible:
            return None
        return round(self.score / self.score_possible * 100, 1)
