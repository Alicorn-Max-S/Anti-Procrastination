"""Decision fields for the hand-labelling pass, shared with the Glimmer probe.

The labelling site and the probe must offer exactly the same choices in the same order, so both
import them from here. An Option is (value, label, description): `value` is what labels.jsonl
stores and what a probe should ask the model to answer with; `label` is the wording on the button;
`description` is the definition the labels follow, shown to every model after the label and on the
labelling page as the button's tooltip. Changing a description changes the prompts, so bump
PROMPT_VERSION (decider/config.py) and recalibrate.

A field's `examples` are worked cases for the boundaries the definitions leave open: an item in a
line, then its answer. Every model sees all of them in the fixed system turn (decider/prompts.py),
so they cost nothing once cached. An example taken from a labeled item names it as `source`, and
the eval harness leaves that label out of scoring so the example can't grade itself. The same rule
as the descriptions applies: a change is a new PROMPT_VERSION and a recalibration.

A field's `rules` are lines that settle what the definitions leave in conflict (which option wins
when two fit). They go under the field's question in the same system turn, ahead of its examples.

Topic is the one field whose options depend on the class. Use `topic_options` to build them: the
class's topics, then "None of these fit".
"""

from typing import NamedTuple


class Option(NamedTuple):
    value: str
    label: str
    description: str = ""


class Example(NamedTuple):
    item: str  # the item in a line: what the model would read, cut to what decides it
    answer: str  # one of the field's option values
    source: str = ""  # the labels.jsonl key it was taken from ("" for a made-up case)


class Field(NamedTuple):
    key: str
    question: str
    options: tuple[Option, ...]  # empty when the options come from the class (see per_class)
    per_class: bool = False
    examples: tuple[Example, ...] = ()
    rules: tuple[str, ...] = ()


ASSIGNMENT_KIND = "assignment"
TEST_QUESTION_KIND = "test_question"

ASSIGNMENT_FIELDS: tuple[Field, ...] = (
    Field("type", "What type of assignment is this?", (
        Option("test_or_quiz", "Test or quiz",
               "a test, quiz, final, reading check (RC), exit ticket from a STEM class or graded "
               "check-in the student studies for, including an exit ticket that's a mini quiz on a "
               "named skill. A review sheet for a test is a worksheet, not the test"),
        Option("essay_or_written_response", "Essay or written response",
               "an essay, draft or written answers the student turns in, including answers about a "
               "reading, video or podcast. Notes don't count, even when they're turned in or shown to "
               "the teacher"),
        Option("reading", "Reading",
               "a text to read (a chapter, article or source), maybe with notes or guiding questions. "
               "Notes keep it a reading even when they're turned in or shown to the teacher; written "
               "answers make it Essay or written response"),
        Option("problem_set_or_worksheet", "Problem set or worksheet",
               "practice problems or worksheet questions to work through, including a review sheet "
               "for a test, and any series of questions answered on their own rather than about a "
               "reading (a coding notebook counts)"),
        Option("lab_report", "Lab report", "a lab and/or its write-up"),
        Option("project_or_presentation", "Project or presentation",
               "a project, presentation or poster with several parts, or a real piece of one like a "
               "draft or the presentation. An idea, topic pick, research question or sign-up for a "
               "project is Something else"),
        Option("video_or_audio", "Video or audio",
               "a video or podcast to watch or listen to, maybe with notes (even notes shown to the "
               "teacher), and no written answers to turn in"),
        Option("something_else", "Something else",
               "anything else: a form, survey, signature, participation grade, test corrections, a "
               "class discussion, something to bring to class, a quick reflection exit ticket, a "
               "small check-in on a project, first-day paperwork (reading the syllabus and filling "
               "out a sheet about yourself), or organizing notes, a journal or a binder (including "
               "an ungraded review-and-organize packet)"),
    ), rules=(
        "A part of a multi-part project stays Project or presentation, even when that part is a "
        "reading.",
    ), examples=(
        Example('Advanced Calculus, "Exit Ticket #1: Continuity @ a Point", 5 points, no description',
                "test_or_quiz", "assignment:19761"),
        Example('Spanish III, "Q: Conjugar en PRESENTE + vocab FAMILIA", 100 points ("Q:" marks a quiz)',
                "test_or_quiz", "assignment:6037"),
        Example('Data Science I, "Simulation Assessment", 25 points: a paper part and a Colab part',
                "test_or_quiz", "assignment:7895"),
        Example('Chemistry, "CW: Drops on a Penny": "You will complete this quick lab exploration in '
                'class."', "lab_report", "assignment:14582"),
        Example('US History, "Founding documents we don\'t learn about": 40 minutes working with primary '
                'documents from the founding era', "reading", "assignment:21748"),
        Example('US History, "Part III: Strands of Democracy -- Culminating Conversations": the last part '
                'of a multi-part project', "project_or_presentation", "assignment:22081"),
        Example('Data Science I, "Final Project Idea": submit the idea for your final project',
                "something_else", "assignment:8363"),
        Example('Advanced Physics, "Test Review: Vectors": review problems before the vectors test',
                "problem_set_or_worksheet", "assignment:24463"),
        # Made up, so no more labels are held out of scoring.
        Example('Chemistry, "HW 3.2 Video: Balancing Equations": "Watch the video and take notes."',
                "video_or_audio"),
        Example('US History, "Podcast response": "Listen to the episode, then answer the three questions '
                'below."', "essay_or_written_response"),
        Example('Chemistry, "HW (optional): Test Corrections for Test 2": correct the questions you missed '
                'to earn back points', "something_else"),
        Example('Spanish, "Foto familiar": "Trae una foto de tu familia a clase." (bring a photo to class)',
                "something_else"),
        Example('US History, "Federalist No. 10": "Read the excerpt and answer the four questions on the '
                'handout."', "essay_or_written_response"),
    )),
    # The prep doc's four templates, with the ids static/prepdoc.js uses (PD_TEMPLATES), then Quick
    # task: one small action with nothing to prep (a form, a signature), which the app shows as a
    # one-line card instead of a template (PrepCard.quick). Focus mode is not one of them: it opens
    # from any template.
    Field("template", "Which prep doc template fits best?", (
        Option("document", "Document",
               "a document or linked documents to read through; the usual pick for a reading in a "
               "humanities class (English or history) that's about the class's subject, and for a "
               "podcast or other audio"),
        Option("checklist", "Checklist",
               "steps to tick off one by one; the usual pick for problem sets, worksheets, projects, "
               "journals or responses written on the spot with no prep time, and shorter writing with "
               "a clear list of requirements"),
        Option("dashboard", "Dashboard",
               "built from the student's graded work and weak spots; always the pick for a test or "
               "quiz (including an exit ticket the student preps for, like a mini quiz) and for labs, "
               "and also for essays (including timed in-class writing like quick writes), "
               "participation grades, and projects done mainly in class"),
        Option("cornell", "Cornell",
               "Cornell notes: cue questions beside notes and a summary, for a lesson, a video, or "
               "any other reading: a science or other non-humanities reading, a syllabus, or "
               "assignment instructions to look over"),
        Option("quick", "Quick task",
               "one small action with nothing to prep: a form, survey (including a post-test survey), "
               "signature, reflection exit ticket, reminder to submit missing work, or a project idea, "
               "research question or sign-up"),
    ), examples=(
        Example('English 10, "Crucible RC 3", 25 points: a reading check, which is a quiz on the reading',
                "dashboard", "assignment:16054"),
        Example('Advanced Calculus, "Exit Ticket #3: Mean Value Theorem", 5 points: "In-Class Mini Quiz"',
                "dashboard", "assignment:19763"),
        Example('English 10, "Harkness Discussion: Funny in Farsi", 100 points: a graded class discussion '
                'to prepare quotes and questions for', "dashboard", "assignment:5242"),
        Example('Chemistry, "HW (optional): Test Corrections for Test 3": guidelines for correcting a '
                'graded test', "checklist", "assignment:8501"),
        Example('Advanced Calculus, "Sine Angle Addition Identity Proof", 5 points: watch a video and take '
                'notes on it', "cornell", "assignment:19785"),
        Example('English 10, "Essay 2 final draft", due as homework', "dashboard"),
        Example('Chemistry, "Read 4.2 on ionic bonding" (3 pages)', "cornell"),
        Example('English 10, "Amy Tan Journal", 25 points: pick a question about "Mother Tongue" and write '
                'a paragraph or two', "checklist", "assignment:11472"),
        Example('US History, "Quick write #1 – Madison case study", 10 points: pick 1 of 3 prompts and '
                'write for 8–10 minutes in class', "dashboard", "assignment:23163"),
    )),
    Field("effort", "How much effort will it take?", (
        Option("one_short_sitting", "One short sitting",
               "a simple assignment, a longer one that was worked on in class or would be finished in "
               "class, a reflection exit ticket, or one with a small number of questions; a problem "
               "set is usually short when it's a link to something like Khan Academy or only a few "
               "questions"),
        Option("one_solid_session", "One solid session",
               "a longer assignment, a lab (even one done in class), a problem set that's a document "
               "or has many questions, a coding notebook, or a reading that needs annotation or is for "
               "a non-STEM class"),
        Option("two_or_three_sessions", "Two or three sessions",
               "spread over two or three sittings, like studying for a quiz, mini-quiz exit ticket, "
               "reading check, pretest or assessment (not a test) or a short project with a few parts"),
        Option("multi_day", "Multi-day",
               "worked on across many days, like studying for a test/final, an essay, in-class work "
               "the student can prepare for, or a long project"),
    ), rules=(
        "Effort goes by what the item is, not by minutes. For anything the student studies or preps "
        "for, the prep decides, even when the item has few questions or happens in class: a quiz, "
        "reading check or mini-quiz exit ticket is Two or three sessions; a test is Multi-day, and "
        "so is in-class work the student can prepare for, like an in-class writing (not a quick "
        "write) or a graded discussion. A separate assignment to prepare for it goes by its own size.",
        "An essay (an assignment called an essay) is Multi-day. A piece of one, like a single body "
        "paragraph, and shorter writing like a journal, response or poem go by their size.",
        "A project or presentation is Two or three sessions or Multi-day, by its scale. A piece of "
        "one that's only a reading, a worksheet or a class session goes by that piece.",
        "An assignment that's a link to an online activity (Khan Academy, Delta Math, a form) "
        "usually takes less effort than one that's an attached document or packet.",
    ), examples=(
        Example('Advanced Calculus, "IVT & MVT Homework", 5 points: "Complete the handout given to you in '
                'class"', "one_short_sitting", "assignment:19774"),
        Example('Advanced Calculus, "Differentiability of Piecewise Functions", 5 points: watch a video and '
                'take notes', "one_short_sitting", "assignment:19759"),
        Example('US History, "MLK Case Study": read a case study before a full-period discussion',
                "one_solid_session", "assignment:22093"),
        Example('Advanced Physics, "Lab: Vector Force Table", 20 points', "one_solid_session",
                "assignment:24303"),
        Example('Advanced Calculus, "Exit Ticket #2: Algebraic Differentiability", 5 points: an in-class '
                'quiz to study for', "two_or_three_sessions", "assignment:23308"),
        Example('Math Analysis, "Quiz 4", 27 points: "Quiz on Unit 4 so far"', "two_or_three_sessions",
                "assignment:11889"),
    )),
)

QUESTION_FIELDS: tuple[Field, ...] = (
    Field("format", "What format is the question?", (
        Option("multiple_choice", "Multiple choice",
               "pick one of the given choices, including a blank filled with one of a few given "
               "words (such as por, para or X)"),
        Option("true_false", "True/false", "decide whether a statement is true or false"),
        Option("matching", "Matching", "pair items from two lists"),
        Option("fill_in_the_blank", "Fill in the blank",
               "write the missing word in a blank, with no list of choices and no verb given to "
               "conjugate"),
        Option("short_answer", "Short answer",
               "a sentence or a few lines, including a sentence built from given words, or anything "
               "that asks for a specific explanation rather than just providing a prompt"),
        Option("free_response", "Free response", "a longer open answer of a paragraph or more"),
        Option("calculation", "Calculation", "work out a result step by step, showing the work"),
        Option("translation", "Translation", "translate text from one language to another"),
        Option("conjugation", "Conjugation",
               "write a given verb (often in parentheses) in the right form; this beats fill in the "
               "blank"),
        Option("diagram", "Diagram", "draw or label a graph or diagram as the answer"),
    ), examples=(  # made up, so no labeled question is held out of a set this small
        Example('Calculus: "Explain in a complete sentence what the sign of the derivative tells you about '
                'the function."', "short_answer"),
        Example('Calculus: "Is g continuous at x = 1? Use the definition of continuity at a point."',
                "calculation"),
        Example('Spanish, section "Llena los espacios con por, para o X": "Estudio ____ sacar buenas '
                'notas."', "multiple_choice"),
    )),
    Field("topic", "What topic is it testing?", (), per_class=True),
    Field("credit", "How much credit did the student get?", (
        Option("full", "Full",
               "no points lost. On a graded test a question with no marks got full credit, and so "
               "did one whose lost points were given back (-1 then +1) or where the teacher only "
               "wrote a note or correction"),
        Option("partial", "Partial", "some of the question's points lost, not all"),
        Option("none", "None", "all of the question's points lost"),
        Option("cant_tell", "Can't tell",
               "only when the page gives no way to know: the test wasn't graded, or the marks "
               "can't be read"),
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
