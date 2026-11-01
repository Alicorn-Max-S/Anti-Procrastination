"""The decider's HTTP API (spec, Part A). One call per assignment or per graded test.

The backend treats any error or timeout as "decider off" and falls back to its own rules, so errors
here are plain: 503 when llama-server can't be reached, 422 for a request that can't be decided.
"""

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .config import settings
from .decide import Decider, UnknownField
from .llama import MIN_BUILD, Llama, LlamaError, build_number
from .prompts import TopicListFull

app = FastAPI(title="Start Helper decider")
_decider: Decider | None = None


def decider() -> Decider:
    global _decider
    if _decider is None:
        _decider = Decider(Llama(settings.glimmer_url), settings)
    return _decider


class RubricRow(BaseModel):
    criterion: str = ""
    points: float | None = None


class AssignmentIn(BaseModel):
    """What the model sees of an assignment: the labelling page's item shape (extra keys ignored)."""

    model_config = ConfigDict(extra="ignore")

    course: str = ""
    title: str = Field("", validation_alias=AliasChoices("title", "name"))
    due: str | None = None
    points: float | None = None
    submission_types: list[str] = []
    attachments: list[str] = []
    rubric: list[RubricRow] = []
    description: str = ""


class AssignmentRequest(BaseModel):
    assignment: AssignmentIn
    fields: list[str] | None = None
    seed: int | None = None  # fixed sampling seed for the thinking loops (eval runs)


class QuestionIn(BaseModel):
    """One row from Spark's grader extraction. Marks are copied literally ("X", "-2", "3/4")."""

    model_config = ConfigDict(extra="ignore")

    id: str | int
    text: str = ""
    answer: str = ""
    marks: str | float | None = None
    note: str = ""
    points: float | str | None = None
    section: str = ""  # the section's instructions, when the test has sections


class TestRequest(BaseModel):
    class_id: int | str
    topics: list[str] = []
    questions: list[QuestionIn]
    fields: list[str] | None = None
    course: str = ""  # the class name, shown first in every question's prompt when given
    seed: int | None = None


class TraceEntry(BaseModel):
    think_tokens: int
    probs: dict[str, float]
    letter_share: float


class Decision(BaseModel):
    value: str
    option_index: int
    confidence: float
    letter_share: float
    flipped: bool | None
    tier: Literal["instant", "thought", "guessed"]
    think_tokens: int
    probe_tokens: int
    trace: list[TraceEntry]
    prompt_version: str
    model: str


class AssignmentResponse(BaseModel):
    decisions: dict[str, Decision]
    timings: dict[str, int]


class TestResponse(BaseModel):
    decisions: dict[str, dict[str, Decision]]
    new_topics: list[str]
    timings: dict[str, int]


@app.get("/health")
def health() -> dict:
    llama = decider().llama
    llama_ok = llama.healthy()
    build = ""
    if llama_ok:
        try:
            build = llama.build()
        except LlamaError:
            llama_ok = False
    return {"ok": llama_ok and build_number(build) >= MIN_BUILD, "llama_ok": llama_ok, "build": build}


def _run(fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except (UnknownField, TopicListFull) as e:
        raise HTTPException(422, str(e)) from e
    except LlamaError as e:
        raise HTTPException(503, f"llama-server: {e}") from e


@app.post("/decide/assignment", response_model=AssignmentResponse)
def decide_assignment(body: AssignmentRequest) -> dict:
    return _run(decider().decide_assignment, body.assignment.model_dump(), body.fields, body.seed)


@app.post("/decide/test", response_model=TestResponse)
def decide_test(body: TestRequest) -> dict:
    ids = [str(q.id) for q in body.questions]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "question ids must be unique")
    questions = [q.model_dump() for q in body.questions]
    return _run(decider().decide_test, body.topics, questions, body.fields, body.course, body.seed)
