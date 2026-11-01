"""Decision fields for the hand-labelling pass, shared with the Glimmer probe.

The labelling site and the probe must offer exactly the same choices in the same order, so both
import them from here. An Option is (value, label): `value` is what labels.jsonl stores and what a
probe should ask the model to answer with; `label` is the wording on the button.

Topic is the one field whose options depend on the class. Use `topic_options` to build them: the
class's topics, then "None of these fit".
"""

from typing import NamedTuple


class Option(NamedTuple):
    value: str
    label: str


class Field(NamedTuple):
    key: str
    question: str
    options: tuple[Option, ...]  # empty when the options come from the class (see per_class)
    per_class: bool = False


ASSIGNMENT_KIND = "assignment"
TEST_QUESTION_KIND = "test_question"

ASSIGNMENT_FIELDS: tuple[Field, ...] = (
    Field("type", "What type of assignment is this?", (
        Option("test_or_quiz", "Test or quiz"),
        Option("essay_or_written_response", "Essay or written response"),
        Option("reading", "Reading"),
        Option("problem_set_or_worksheet", "Problem set or worksheet"),
        Option("lab_report", "Lab report"),
        Option("project_or_presentation", "Project or presentation"),
        Option("something_else", "Something else"),
    )),
    # The prep doc's four templates, with the ids static/prepdoc.js uses (PD_TEMPLATES), then Quick
    # task: one small action with nothing to prep (a form, a signature), which the app shows as a
    # one-line card instead of a template (PrepCard.quick). Focus mode is not one of them: it opens
    # from any template.
    Field("template", "Which prep doc template fits best?", (
        Option("document", "Document"),
        Option("checklist", "Checklist"),
        Option("dashboard", "Dashboard"),
        Option("cornell", "Cornell"),
        Option("quick", "Quick task"),
    )),
    Field("effort", "How much effort will it take?", (
        Option("one_short_sitting", "One short sitting"),
        Option("one_solid_session", "One solid session"),
        Option("two_or_three_sessions", "Two or three sessions"),
        Option("multi_day", "Multi-day"),
    )),
)

QUESTION_FIELDS: tuple[Field, ...] = (
    Field("format", "What format is the question?", (
        Option("multiple_choice", "Multiple choice"),
        Option("true_false", "True/false"),
        Option("matching", "Matching"),
        Option("fill_in_the_blank", "Fill in the blank"),
        Option("short_answer", "Short answer"),
        Option("free_response", "Free response"),
        Option("calculation", "Calculation"),
        Option("translation", "Translation"),
        Option("diagram", "Diagram"),
    )),
    Field("topic", "What topic is it testing?", (), per_class=True),
    Field("credit", "How much credit did the student get?", (
        Option("full", "Full"),
        Option("partial", "Partial"),
        Option("none", "None"),
        Option("cant_tell", "Can't tell"),
    )),
)

FIELDS_BY_KIND: dict[str, tuple[Field, ...]] = {
    ASSIGNMENT_KIND: ASSIGNMENT_FIELDS,
    TEST_QUESTION_KIND: QUESTION_FIELDS,
}

NONE_FIT = Option("none_fit", "None of these fit")  # asks for a new topic name; never stored as a label


def normalize_topic(name: str) -> str:
    """Same spelling the grader uses for topics: lower case, words joined by underscores."""
    return "_".join(name.strip().lower().split())


def topic_options(topics: list[str]) -> list[Option]:
    """A class's topic options in display order, followed by "None of these fit"."""
    return [Option(t, t.replace("_", " ")) for t in topics] + [NONE_FIT]


def field_values(field: Field) -> list[str]:
    return [o.value for o in field.options]
