"""Client for the System One decider (decider/, `python -m decider`): local Muse Glimmer makes the
categorical calls, so Spark doesn't.

- An assignment gets its type, template and effort. Glimmer thinks on the ones it is unsure about
  (the decider's fields mode), so this takes seconds. The type becomes the card's kind, the effort
  its block size, and the template the prep doc that opens first.
- A graded test's questions get a format, topic and credit each, with thinking off: the fast pass
  alone, a second or so per question. Format becomes the row's `qtype`, which the topic-by-type grid
  and the practice sets use.

Any failure (the decider isn't running, llama-server is down, a timeout) returns None, and the
caller keeps what it did before Glimmer: the core app works with the decider off. The wire shapes
are the labeling page's items, so Glimmer reads what it was calibrated on (labeling/server.py
`assignment_item`, eval/items.py `page_rows`).
"""

import logging
import threading
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import httpx

from app import canvas
from app.clock import as_local
from app.config import settings
from app.models import Assignment, AssignmentKind, Effort

log = logging.getLogger(__name__)

ASSIGNMENT_TIMEOUT = 90.0  # seconds for one assignment's turn at the decider
TEST_TIMEOUT = 600.0  # and for one whole test
MAX_TOPICS = 25  # the decider's most topics per class: one letter each, plus "none of these fit"

# Glimmer's type -> the app's card kind. A project, a video and "something else" have no kind of
# their own; their template (and the card's own steps) is what sets them apart.
KIND: dict[str, AssignmentKind] = {
    "test_or_quiz": "test",
    "essay_or_written_response": "essay",
    "reading": "reading",
    "problem_set_or_worksheet": "problem_set",
    "lab_report": "lab",
}
# Four effort levels onto the app's three block sizes (scheduler.DURATION_MINUTES).
EFFORT: dict[str, Effort] = {
    "one_short_sitting": "light",
    "one_solid_session": "medium",
    "two_or_three_sessions": "heavy",
    "multi_day": "heavy",
}
TEMPLATES = ("document", "checklist", "dashboard", "cornell")  # static/prepdoc.js PD_TEMPLATES

# The decider answers one item at a time, and a sync builds several cards at once. Waiting here
# keeps each item's timeout to its own turn instead of the whole queue's.
_turn = threading.Lock()


def configured() -> bool:
    return bool(settings.decider_url)


def _client(timeout: float) -> httpx.Client:
    """One place to build the HTTP client, so tests can swap in a mock transport."""
    return httpx.Client(base_url=settings.decider_url, timeout=timeout)


def _post(path: str, body: dict, timeout: float) -> dict | None:
    if not configured():
        return None
    with _turn:
        try:
            with _client(timeout) as client:
                r = client.post(path, json=body)
            if r.status_code >= 400:
                log.warning("decider %s: %s %s", path, r.status_code, r.text[:200])
                return None
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("decider %s unavailable: %s", path, e)
            return None


def healthy() -> bool:
    """Whether the decider and the llama-server behind it answer (for /api/health)."""
    if not configured():
        return False
    try:
        with _client(3.0) as client:
            return bool(client.get("/health").json().get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


def _record(d: dict) -> dict:
    """What a card or row keeps of a decision."""
    return {
        "value": d["value"],
        "tier": d["tier"],
        "confidence": round(d.get("calibrated_confidence", d.get("confidence", 0.0)), 3),
        "clarify": bool(d.get("clarify")),
    }


# ---- assignments ------------------------------------------------------------------------------


class _FileLinks(HTMLParser):
    """Names of the files linked from an assignment description (Canvas file links)."""

    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self._open = False
        self._text: list[str] = []
        self._fallback = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("data-api-returntype") == "File":
            self._open, self._text = True, []
            self._fallback = a.get("title") or a.get("data-api-endpoint") or ""

    def handle_data(self, data):
        if self._open:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            self._open = False
            name = " ".join("".join(self._text).split()) or self._fallback.rstrip("/").rsplit("/", 1)[-1]
            if name:
                self.names.append(name)


def attachment_names(a: Assignment) -> list[str]:
    folder = Path(settings.files_dir) / "attachments" / str(a.id)
    if folder.is_dir():  # already downloaded: those are the real names
        return sorted(p.name for p in folder.iterdir() if p.is_file())
    links = _FileLinks()
    links.feed(a.description_html or "")
    return list(dict.fromkeys(links.names))


def assignment_payload(a: Assignment, course: str) -> dict:
    """What Glimmer sees of an assignment. No guessed kind, score or feedback."""
    due = as_local(a.due_at).strftime("%a %b %d, %Y, %I:%M %p").replace(" 0", " ") if a.due_at else None
    return {
        "course": course,
        "title": a.name,
        "due": due,
        "points": a.points_possible,
        "submission_types": a.submission_types,
        "attachments": attachment_names(a),
        "rubric": [{"criterion": r.criterion, "points": r.points_possible} for r in a.rubric],
        "description": canvas.parse_description(a.description_html)[0],
    }


@dataclass
class AssignmentDecision:
    kind: AssignmentKind
    effort: Effort
    template: str  # one of TEMPLATES, or "" for a quick task
    quick: bool
    records: dict[str, dict]  # field -> {value, tier, confidence, clarify}


def decide_assignment(a: Assignment, course: str) -> AssignmentDecision | None:
    out = _post("/decide/assignment", {"assignment": assignment_payload(a, course)}, ASSIGNMENT_TIMEOUT)
    if not out:
        return None
    try:
        got = out["decisions"]
        template = got["template"]["value"]
        decision = AssignmentDecision(
            kind=KIND.get(got["type"]["value"], "other"),
            effort=EFFORT[got["effort"]["value"]],
            template=template if template in TEMPLATES else "",
            quick=template == "quick",
            records={k: _record(v) for k, v in got.items()},
        )
    except (KeyError, TypeError) as e:
        log.warning("decider answered an assignment oddly: %s", e)
        return None
    log.info("glimmer decided %r: %s", a.name, {k: (v["value"], v["tier"]) for k, v in decision.records.items()})
    return decision


# ---- graded tests -----------------------------------------------------------------------------


def decide_test(course_id: int, course: str, topics: list[str], questions: list[dict]) -> list[dict[str, dict]] | None:
    """One {format, topic, credit} of decisions per question, in order, thinking off. `questions`
    are the decider's rows (text, answer, marks, note, points, section); they get ids here."""
    if not questions:
        return []
    body = {
        "class_id": course_id,
        "course": course,
        "topics": topics[:MAX_TOPICS],
        "questions": [{**q, "id": f"q{i}"} for i, q in enumerate(questions)],
        "think": False,
    }
    out = _post("/decide/test", body, TEST_TIMEOUT)
    if not out:
        return None
    try:
        decided = out["decisions"]
        return [{k: _record(v) for k, v in decided[f"q{i}"].items()} for i in range(len(questions))]
    except (KeyError, TypeError) as e:
        log.warning("decider answered a test oddly: %s", e)
        return None
