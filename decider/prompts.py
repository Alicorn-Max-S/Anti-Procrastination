"""Prompts: the fixed system turn, what the model sees of an item, and the lettered question.

The user turn goes from most shared to least shared so llama-server keeps the shared part cached:
item context first, then the question block. The question and option wording come from
decisions/fields.py, the same choices the labelling page offers.
"""

import string
from dataclasses import dataclass

from decisions.fields import ASSIGNMENT_FIELDS, NONE_FIT, QUESTION_FIELDS, Field, Option, topic_options

from .llama import Llama

# Byte-identical in every request: no timestamps or per-request data, and never a different
# reasoning strength, or the cached prefix is thrown away.
SYSTEM = (
    "You are the decision layer of Start Helper, a tool that turns a high school student's Canvas "
    "assignments and graded tests into prep cards and practice. You get one assignment, or one "
    "question from a graded test, and one multiple-choice question about it. Answer with the "
    "letter of the best option and nothing else.\n\n"
    "Reasoning strength: low."
)
THINK_OPEN = "<|start|>assistant to=self<|message|>"
THINK_CLOSE = "<|eom|>"
ANSWER = "<|start|>assistant to=user<|message|>"
REPLY = "Reply with just the letter."

LETTERS = string.ascii_uppercase
MAX_TOPICS = len(LETTERS) - 1  # 25 topics plus "None of these fit", one letter each
DESCRIPTION_CHARS = 1200

# The template hint: the prep doc each assignment type usually gets. Lab reports and projects get
# theirs once Max fills them in (spec, Status section).
TEMPLATE_DEFAULTS = {
    "test_or_quiz": "dashboard",
    "essay_or_written_response": "dashboard",
    "reading": "document",
    "problem_set_or_worksheet": "checklist",
}

FIELDS = {f.key: f for f in ASSIGNMENT_FIELDS + QUESTION_FIELDS}


class TopicListFull(ValueError):
    """A class has more topics than there are letters for."""


def _label(field_key: str, value: str) -> str:
    return next(o.label for o in FIELDS[field_key].options if o.value == value)


def template_hint() -> str:
    pairs = "; ".join(f"{_label('type', t)}: {_label('template', d)}" for t, d in TEMPLATE_DEFAULTS.items())
    return f"Usual template by assignment type: {pairs}."


@dataclass(frozen=True)
class Choice:
    """One field's question, with its options lettered in `order` (indexes into `options`)."""

    field: str
    question: str
    options: tuple[Option, ...]  # in the field's own order: option_index points in here
    order: tuple[int, ...]
    last_fixed: bool = False  # "None of these fit" stays last when the order is reversed

    def reversed(self) -> "Choice":
        order = self.order[:-1][::-1] + self.order[-1:] if self.last_fixed else self.order[::-1]
        return Choice(self.field, self.question, self.options, order, self.last_fixed)

    def block(self) -> str:
        lines = [f"Question: {self.question}"]
        lines += [f"{LETTERS[pos]}) {self.options[i].label}" for pos, i in enumerate(self.order)]
        lines.append(REPLY)
        return "\n".join(lines)


def choice_for(field: Field, topics: list[str] | None = None) -> Choice:
    if field.per_class:
        topics = topics or []
        if len(topics) > MAX_TOPICS:
            raise TopicListFull(f"{len(topics)} topics; at most {MAX_TOPICS} fit one letter each")
        options = tuple(topic_options(topics))
    else:
        options = field.options
    question = field.question
    if field.key == "template":
        question = f"{question}\n{template_hint()}"
    return Choice(field.key, question, options, tuple(range(len(options))), field.per_class)


def none_fit_index(choice: Choice) -> int | None:
    return len(choice.options) - 1 if choice.last_fixed and choice.options[-1] == NONE_FIT else None


# ---- what the model sees of an item -----------------------------------------------------------


def _num(x) -> str:
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def assignment_context(a: dict) -> str:
    points = a.get("points")
    rubric = a.get("rubric") or []
    lines = [
        f"Course: {a.get('course') or 'not stated'}",
        f"Assignment: {a.get('title') or 'untitled'}",
        f"Due: {a.get('due') or 'no due date'}",
        f"Points: {'not graded' if points is None else _num(points)}",
        f"Submission: {', '.join(a.get('submission_types') or []) or 'not stated'}",
        f"Attachments: {', '.join(a.get('attachments') or []) or 'none'}",
    ]
    if rubric:
        lines.append("Rubric:")
        for r in rubric:
            pts = r.get("points")
            lines.append(f"- {r.get('criterion', '')}" + ("" if pts is None else f" ({_num(pts)} pts)"))
    else:
        lines.append("Rubric: none")
    lines += ["Description:", (a.get("description") or "")[:DESCRIPTION_CHARS] or "(none)"]
    return "\n".join(lines)


def test_prefix(topics: list[str], course: str = "") -> str:
    """The part shared by every question of a test: the format list and the class's topics."""
    formats = ", ".join(o.label for o in FIELDS["format"].options)
    lines = [f"Class: {course}"] if course else []
    lines += [f"Question formats: {formats}", "Class topics:"]
    lines += [f"- {o.label}" for o in topic_options(topics)[:-1]] or ["(none yet)"]
    return "\n".join(lines)


def question_row(q: dict) -> str:
    lines = ["Graded test question:"]
    if q.get("section"):
        lines.append(f"Section: {q['section']}")
    lines += [
        f"Question: {q.get('text') or '(not readable)'}",
        f"Student's answer: {q.get('answer') or '(blank)'}",
        f"Marks as written: {_num(q['marks']) if q.get('marks') not in (None, '') else '(none)'}",
        f"Teacher note: {q.get('note') or '(none)'}",
        f"Points: {_num(q['points']) if q.get('points') not in (None, '') else 'not shown'}",
    ]
    return "\n".join(lines)


def test_context(q: dict, topics: list[str], course: str = "") -> str:
    return f"{test_prefix(topics, course)}\n\n{question_row(q)}"


# ---- token ids --------------------------------------------------------------------------------


class Prompt:
    """One item with one question block, as token ids ready for a thinking turn or the answer."""

    def __init__(self, llama: Llama, context: str, block: str, fast_format: str) -> None:
        self.llama = llama
        self.fast_format = fast_format
        rendered = llama.render([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{context}\n\n{block}"},
        ])
        self.turns = llama.tokenize(rendered, bos=True)  # ends with <|eot|>

    def thinking(self, thought: list[int]) -> list[int]:
        """Where Glimmer goes on thinking: the open thinking turn and the thinking so far."""
        return self.turns + self.llama.const(THINK_OPEN) + thought

    def answer(self, thought: list[int]) -> list[int]:
        """The answer turn opener, after the thinking turn (left out for the no-thinking fast pass)."""
        if not thought and self.fast_format == "no-thinking":
            return self.turns + self.llama.const(ANSWER)
        return self.thinking(thought) + self.llama.const(THINK_CLOSE + ANSWER)
