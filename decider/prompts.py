"""Prompts: the fixed system turn, what the model sees of an item, and the lettered question.

The user turn goes from most shared to least shared so llama-server keeps the shared part cached:
item context first, then the question block. The question and option wording come from
decisions/fields.py, the same choices the labelling page offers.
"""

import string
from dataclasses import dataclass

from decisions.fields import ASSIGNMENT_FIELDS, NONE_FIT, QUESTION_FIELDS, Field, Option, topic_options

from .llama import Llama

ROLE = (
    "You are the decision layer of Start Helper, a tool that turns a high school student's Canvas "
    "assignments and graded tests into prep cards and practice. You get one assignment, or one "
    "question from a graded test, and one multiple-choice question about it. Answer with the "
    "letter of the best option and nothing else."
)
THINK_OPEN = "<|start|>assistant to=self<|message|>"
THINK_CLOSE = "<|eom|>"
ANSWER = "<|start|>assistant to=user<|message|>"
REPLY = "Reply with just the letter."

LETTERS = string.ascii_uppercase
MAX_TOPICS = len(LETTERS) - 1  # 25 topics plus "None of these fit", one letter each
DESCRIPTION_CHARS = 1200

# The template hint: the prep doc each assignment type usually gets, one line per type in the words
# of Max's Nov 1 rules (spec, Status section).
TEMPLATE_DEFAULTS = {
    "test_or_quiz": "Dashboard",
    "essay_or_written_response": "Dashboard for an essay, timed in-class writing like quick writes, "
                                 "and creative writing worked on over several days or for a major "
                                 "grade; Checklist for journals, responses written on the spot, "
                                 "creative writing done at home for a short deadline, and shorter "
                                 "writing with a clear list of requirements; Document for answers to "
                                 "the questions that come with a reading in a non-STEM class; Cornell "
                                 "when the main work is watching a video",
    "reading": "Cornell if it's read in class; Document if it's for a non-STEM class (like English, "
               "history or a language), about the class's subject and read outside class; otherwise "
               "Cornell (a science or math reading, a syllabus, assignment instructions)",
    "problem_set_or_worksheet": "Checklist",
    "lab_report": "Dashboard",
    "project_or_presentation": "a part centered on a reading follows the reading rule, even when it's "
                               "discussed or presented in class; a class discussion, or a project done "
                               "mainly in class, is Dashboard; otherwise Checklist",
    "video_or_audio": "Cornell for a video, Document for a podcast or other audio",
    "something_else": "Quick task if it's one small action with nothing to prep; Dashboard for a "
                      "participation grade or a journal check; otherwise Checklist, including test "
                      "corrections and getting ready for a test, essay or discussion",
}

# The effort hint, the same way: how long each assignment type usually takes, in the words of Max's
# rules. System One's effort skips thinking, so this next to its type answer is most of what it goes on.
EFFORT_DEFAULTS = {
    "test_or_quiz": "Two or three sessions for a quiz, reading check, mini quiz, check-in or "
                    "assessment; Multi-day for a test or final",
    "essay_or_written_response": "Multi-day for an essay or an in-class essay or writing assessment; "
                                 "One solid session for a single body paragraph; One short sitting "
                                 "for a journal or diario entry; a poem or other creative piece is Two "
                                 "or three sessions, or Multi-day when it's worked on over several days "
                                 "or is a major grade; anything else by its size",
    "reading": "One solid session for a non-STEM reading or one that needs annotation, even one read "
               "in class; One short sitting only for a short reading in a STEM class",
    "problem_set_or_worksheet": "One solid session, unless it was started or handed out in class, is "
                                "a link to an online activity (Khan Academy, Delta Math) or has only a "
                                "few questions: then One short sitting. A coding notebook is One solid "
                                "session",
    "lab_report": "One solid session",
    "project_or_presentation": "Multi-day for a whole unit project or one built on collecting and "
                               "analyzing data, otherwise Two or three sessions; one part of a project "
                               "goes by that part, usually One solid session",
    "video_or_audio": "One short sitting for a video or a set of short ones; One solid session for a "
                      "podcast episode or other long audio",
    "something_else": "One short sitting for a form, survey, exit ticket, participation grade, sign-up, "
                      "research question, checking work against an answer key, something to bring to "
                      "class or other small task; One solid "
                      "session for test corrections; One solid session or Two or three sessions for "
                      "getting ready for a test, essay or discussion; Multi-day for a graded discussion",
}

# Types whose template is never in doubt: with TEMPLATE_FROM_TYPE on, the decider skips the template
# question for them and takes this (tier "default"). Same picks as their lines above.
TEMPLATE_FROM_TYPE = {
    "test_or_quiz": "dashboard",
    "problem_set_or_worksheet": "checklist",
    "lab_report": "dashboard",
}

# Asked after type, with System One's own type answer in the prompt: base Glimmer and Spark answer
# every field in one reply, so they see their own type answer by the time they reach these.
USES_TYPE = ("template", "effort")

FIELDS = {f.key: f for f in ASSIGNMENT_FIELDS + QUESTION_FIELDS}


class TopicListFull(ValueError):
    """A class has more topics than there are letters for."""


def _label(field_key: str, value: str) -> str:
    return next(o.label for o in FIELDS[field_key].options if o.value == value)


def examples_block() -> str:
    """Every field's rules, then its worked examples (decisions/fields.py), under the field's question."""
    lines = ["Rules and worked examples (the item, then the answer):"]
    for f in ASSIGNMENT_FIELDS + QUESTION_FIELDS:
        if f.examples or f.rules:
            lines += ["", f.question] + [f"Rule: {r}" for r in f.rules]
            lines += [f"- {e.item} → {_label(f.key, e.answer)}" for e in f.examples]
    return "\n".join(lines)


# Byte-identical in every request: no timestamps or per-request data, and never a different
# reasoning strength, or the cached prefix is thrown away. The examples sit here, in the prefix every
# request shares, so llama-server reads them once.
SYSTEM = f"{ROLE}\n\n{examples_block()}\n\nReasoning strength: low."


def _hint(what: str, defaults: dict[str, str]) -> str:
    lines = "\n".join(f"- {_label('type', t)}: {line}" for t, line in defaults.items())
    return f"Usual {what} by assignment type:\n{lines}"


def template_hint() -> str:
    return _hint("template", TEMPLATE_DEFAULTS)


def effort_hint() -> str:
    return _hint("effort", EFFORT_DEFAULTS)


def option_text(o: Option) -> str:
    """An option as every model sees it: the button's label, then the definition the labels follow."""
    return f"{o.label}: {o.description}" if o.description else o.label


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
        lines += [f"{LETTERS[pos]}) {option_text(self.options[i])}" for pos, i in enumerate(self.order)]
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
    elif field.key == "effort":
        question = f"{question}\n{effort_hint()}"
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


def with_type(context: str, type_value: str) -> str:
    """The item as template and effort see it: the type answer goes after the shared context, so
    the cached prefix stays the same for every field."""
    return f"{context}\n\nAssignment type: {_label('type', type_value)}"


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
