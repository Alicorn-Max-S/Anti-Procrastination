"""Fabricated demo data, so the app runs without Canvas or a model key (and for the video).

`python -m app.demo` loads four made-up courses with graded history, upcoming work, prep cards
and graded-test rows, dated relative to the (possibly simulated) clock, then plans the week.
`python -m app.demo --usage` also adds three made-up weeks of pings, answers and starts (see
USAGE), the fallback for the video's starting stats if real use is thin; say so in the submission.
`python -m app.demo --clear` removes it again. Demo ids sit in 9_000_000..9_099_999 so they never
collide with real Canvas ids. Nothing here is real student data.
"""

import sys
from datetime import datetime, timedelta

from app import db, loop
from app.clock import clock, local_tz
from app.models import (
    Assignment,
    Block,
    Course,
    Link,
    Ping,
    PracticeQuestion,
    PrepCard,
    RubricScore,
    SubmissionComment,
    TestRow,
)

ID_MIN, ID_MAX = 9_000_000, 9_099_999

BIO, SPANISH, ALGEBRA, ENGLISH = 9_000_001, 9_000_002, 9_000_003, 9_000_004
COURSES = [
    Course(id=BIO, name="AP Biology (demo)"),
    Course(id=SPANISH, name="Spanish 3 (demo)"),
    Course(id=ALGEBRA, name="Algebra II (demo)"),
    Course(id=ENGLISH, name="English 10 (demo)"),
]


def _day(now: datetime, days: int, hour: int = 23, minute: int = 59) -> datetime:
    local = now.astimezone(local_tz()) + timedelta(days=days)
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _assignments(now: datetime) -> list[Assignment]:
    lab3, ps5, rr1 = 9_000_101, 9_000_102, 9_000_103
    return [
        # ---- graded history: where the watch-out lines come from ----
        Assignment(
            id=lab3, course_id=BIO, name="Lab 3: Enzyme Activity", kind="lab", submitted=True,
            due_at=_day(now, -12), graded_at=_day(now, -8, 16, 0), points_possible=20, score=15,
            rubric=[
                RubricScore(assignment_id=lab3, criterion_id="c1", criterion="Hypothesis", points=4, points_possible=4),
                RubricScore(
                    assignment_id=lab3, criterion_id="c2", criterion="Observations", points=2, points_possible=5,
                    comment="Record what you actually saw: color changes, timing, bubbles. 'It reacted' isn't an observation.",
                ),
                RubricScore(assignment_id=lab3, criterion_id="c3", criterion="Data table", points=4, points_possible=5, comment="Units missing in column 3."),
                RubricScore(assignment_id=lab3, criterion_id="c4", criterion="Conclusion", points=5, points_possible=6),
            ],
            comments=[
                SubmissionComment(id=9_000_901, assignment_id=lab3, author="Ms. Rivera (demo)", text="Good analysis, but the observations section was thin again."),
            ],
        ),
        Assignment(
            id=ps5, course_id=ALGEBRA, name="Problem Set 5: Exponents", kind="problem_set", submitted=True,
            due_at=_day(now, -9), graded_at=_day(now, -6, 17, 0), points_possible=20, score=16,
            rubric=[
                RubricScore(
                    assignment_id=ps5, criterion_id="c1", criterion="Work shown", points=3, points_possible=5,
                    comment="Show each step. I can't give partial credit for answers alone.",
                ),
                RubricScore(assignment_id=ps5, criterion_id="c2", criterion="Correct answers", points=13, points_possible=15),
            ],
        ),
        Assignment(
            id=rr1, course_id=ENGLISH, name="Reading Response: Chapters 4-6", kind="reading", submitted=True,
            due_at=_day(now, -7), graded_at=_day(now, -5, 15, 0), points_possible=10, score=8,
            comments=[
                SubmissionComment(id=9_000_902, assignment_id=rr1, author="Mr. Chen (demo)", text="Strong summary, but your claim needs a quote to back it up."),
            ],
        ),
        # ---- upcoming work ----
        Assignment(
            id=9_000_201, course_id=BIO, name="Lab 4: Osmosis in Potato Cores", kind="lab",
            due_at=_day(now, 2), points_possible=20,
            description_html=(
                "<p>Measure how salt concentration changes the mass of potato cores over 24 hours.</p>"
                "<p>Use the <a href=\"https://example.com/demo/osmosis-handout.pdf\">lab handout</a> and record "
                "your data in the <a href=\"https://example.com/demo/osmosis-data-sheet\">shared data sheet</a>.</p>"
                "<p>Submit a full lab report: hypothesis, observations, data table, graph, conclusion.</p>"
            ),
            html_url="https://example.com/demo/courses/bio/assignments/lab4",
            rubric=[
                RubricScore(assignment_id=9_000_201, criterion_id="c1", criterion="Hypothesis", points_possible=4),
                RubricScore(assignment_id=9_000_201, criterion_id="c2", criterion="Observations", points_possible=5),
                RubricScore(assignment_id=9_000_201, criterion_id="c3", criterion="Data table and graph", points_possible=5),
                RubricScore(assignment_id=9_000_201, criterion_id="c4", criterion="Conclusion", points_possible=6),
            ],
        ),
        Assignment(
            id=9_000_202, course_id=SPANISH, name="Quiz: Preterite vs. Imperfect", kind="test",
            due_at=_day(now, 3, 9, 0), points_possible=25,
            description_html="<p>Quiz on preterite vs. imperfect, irregular preterite verbs, and unit 4 vocabulary.</p>",
            html_url="https://example.com/demo/courses/spanish/assignments/quiz4",
        ),
        Assignment(
            id=9_000_203, course_id=ALGEBRA, name="Problem Set 6: Logarithms", kind="problem_set",
            due_at=_day(now, 1), points_possible=20,
            description_html="<p>Textbook section 6.2, problems 1-19 odd. Show all work.</p>",
            html_url="https://example.com/demo/courses/algebra/assignments/ps6",
        ),
        Assignment(
            id=9_000_204, course_id=ENGLISH, name="Reading Response: Chapters 7-9", kind="reading",
            due_at=_day(now, 4), points_possible=10,
            description_html="<p>In 250 words: how does the setting shape the main character's choices in chapters 7-9? Use at least one quote.</p>",
            html_url="https://example.com/demo/courses/english/assignments/rr3",
        ),
        Assignment(
            id=9_000_205, course_id=ENGLISH, name="Argument Essay: First Draft", kind="essay",
            due_at=_day(now, 6), points_possible=50,
            description_html="<p>Draft a 3-page argument essay on a topic from our unit. Include a thesis and three pieces of evidence.</p>",
            html_url="https://example.com/demo/courses/english/assignments/essay1",
        ),
    ]


def _cards() -> list[PrepCard]:
    canvas = "Assignment on Canvas"
    return [
        PrepCard(
            assignment_id=9_000_201, kind="lab", effort="heavy", generated_by="demo",
            summary="A full lab report on how salt concentration changes potato core mass over 24 hours.",
            first_step="Open the lab handout and copy its five section headings into a new doc titled 'Lab 4: Osmosis'.",
            steps=[
                "Write a one-sentence hypothesis about mass change vs. salt concentration",
                "While the cores soak, write observations as you go: color, texture, firmness, with times",
                "Fill the data table with units in every column header",
                "Graph % mass change against concentration",
                "Write the conclusion: was the hypothesis supported, and what was the error source?",
            ],
            links=[
                Link(label="lab handout", url="https://example.com/demo/osmosis-handout.pdf"),
                Link(label="shared data sheet", url="https://example.com/demo/osmosis-data-sheet"),
                Link(label=canvas, url="https://example.com/demo/courses/bio/assignments/lab4"),
            ],
            watch_out="On your last lab you lost 3/5 on Observations: “Record what you actually saw: color changes, timing, bubbles.”",
        ),
        PrepCard(
            assignment_id=9_000_202, kind="test", effort="medium", generated_by="demo",
            summary="Quiz on preterite vs. imperfect, irregular preterites and unit 4 vocabulary.",
            first_step="Answer practice question 1 (preterite vs imperfect, fill in blank) without notes.",
            steps=[
                "Do the practice set below without notes",
                "Check answers and reread the explanation for each miss",
                "Write the 'imperfect = background, preterite = event' rule in your own words",
                "Redo the questions you missed tomorrow",
            ],
            links=[Link(label=canvas, url="https://example.com/demo/courses/spanish/assignments/quiz4")],
            watch_out="On both graded quizzes you lost most marks on preterite vs. imperfect in fill-in-the-blank questions.",
            focus="From your graded quizzes, you lose the most marks on preterite vs imperfect (fill in blank) and irregular preterite (translation).",
            practice=[
                PracticeQuestion(
                    topic="preterite_vs_imperfect", qtype="fill_in_blank", verified=True,
                    prompt="Cuando yo ____ (ser) niño, siempre ____ (jugar) en el parque.",
                    answer="era; jugaba",
                    explanation="Both describe background and habit in the past, so both take the imperfect.",
                ),
                PracticeQuestion(
                    topic="preterite_vs_imperfect", qtype="fill_in_blank", verified=True,
                    prompt="Ayer, mientras yo ____ (leer), el teléfono ____ (sonar).",
                    answer="leía; sonó",
                    explanation="The ongoing action (reading) is imperfect; the interruption (the phone rang) is preterite.",
                ),
                PracticeQuestion(
                    topic="preterite_vs_imperfect", qtype="multiple_choice", verified=True,
                    prompt="El sábado pasado nosotros ____ al cine.",
                    choices=["íbamos", "fuimos", "vamos", "iremos"],
                    answer="fuimos",
                    explanation="A single completed event on a specific day takes the preterite.",
                ),
                PracticeQuestion(
                    topic="irregular_preterite", qtype="translation", verified=True,
                    prompt="Translate: She had to leave early.",
                    answer="Ella tuvo que salir temprano.",
                    explanation="Tener is irregular in the preterite: tuve, tuviste, tuvo...",
                ),
                PracticeQuestion(
                    topic="irregular_preterite", qtype="translation", verified=True,
                    prompt="Translate: They brought the food.",
                    answer="Ellos trajeron la comida.",
                    explanation="Traer has a j-stem in the preterite, and the ellos form ends in -eron, not -ieron.",
                ),
            ],
        ),
        PrepCard(
            assignment_id=9_000_203, kind="problem_set", effort="medium", generated_by="demo",
            summary="Section 6.2 logarithm problems 1-19 odd, with all work shown.",
            first_step="Copy problem 1 from section 6.2 onto paper and rewrite it in exponential form.",
            steps=[
                "Do problems 1-7 odd, writing the exponential form first each time",
                "Do 9-15 odd using the log rules; name the rule on each line",
                "Do 17 and 19 and check them by plugging back in",
                "Circle any you're unsure of to ask about",
            ],
            links=[Link(label=canvas, url="https://example.com/demo/courses/algebra/assignments/ps6")],
            watch_out="On Problem Set 5 you lost 2/5 on “Work shown”: “Show each step. I can't give partial credit for answers alone.”",
        ),
        PrepCard(
            assignment_id=9_000_204, kind="reading", effort="light", generated_by="demo",
            summary="A 250-word response on how the setting shapes the main character's choices in chapters 7-9.",
            first_step="Open chapter 7 and flag the first sentence that describes the setting.",
            steps=[
                "Read chapters 7-9, flagging setting descriptions",
                "Pick the one quote that best shows setting driving a choice",
                "Write a one-sentence claim",
                "Write 250 words: claim, quote, explanation",
            ],
            links=[Link(label=canvas, url="https://example.com/demo/courses/english/assignments/rr3")],
            watch_out="Mr. Chen (demo) on your last response: “your claim needs a quote to back it up.”",
        ),
        PrepCard(
            assignment_id=9_000_205, kind="essay", effort="heavy", generated_by="demo",
            summary="A 3-page argument essay draft with a thesis and three pieces of evidence.",
            first_step="Open a new doc titled 'Argument Essay Draft' and write a one-sentence thesis, even a rough one.",
            steps=[
                "Pick a topic and write the thesis",
                "List three pieces of evidence with sources",
                "Outline: intro, three body paragraphs, counterargument, conclusion",
                "Draft one body paragraph per sitting",
                "Reread against the rubric before turning in",
            ],
            links=[Link(label=canvas, url="https://example.com/demo/courses/english/assignments/essay1")],
        ),
    ]


def _test_rows() -> list[TestRow]:
    q = [
        # (source, question, answer, marks, possible, note, topic, qtype)
        ("quiz2-demo.jpg", "1. Mientras nosotros ____ (cenar), llegó mi tío.", "cenamos", 0, 2, "ongoing action → imperfect", "preterite_vs_imperfect", "fill_in_blank"),
        ("quiz2-demo.jpg", "2. De niña, ella ____ (vivir) en Lima.", "vivió", 0, 2, "habitual past!", "preterite_vs_imperfect", "fill_in_blank"),
        ("quiz2-demo.jpg", "3. Ayer yo ____ (comprar) un libro.", "compré", 2, 2, "", "preterite_vs_imperfect", "fill_in_blank"),
        ("quiz2-demo.jpg", "4. el almuerzo = ?", "lunch", 1, 1, "", "vocabulary", "multiple_choice"),
        ("quiz2-demo.jpg", "5. Translate: I went to the store.", "Yo fui a la tienda.", 2, 2, "", "irregular_preterite", "translation"),
        ("quiz2-demo.jpg", "6. Translate: We did the homework.", "Nosotros hacimos la tarea.", 0, 2, "hicimos", "irregular_preterite", "translation"),
        ("quiz3-demo.jpg", "1. Eran las tres cuando ella ____ (salir).", "salía", 0, 2, "single event → preterite", "preterite_vs_imperfect", "fill_in_blank"),
        ("quiz3-demo.jpg", "2. Siempre ____ (hacer) frío en enero.", "hacía", 2, 2, "", "preterite_vs_imperfect", "fill_in_blank"),
        ("quiz3-demo.jpg", "3. la cuchara = ?", "spoon", 1, 1, "", "vocabulary", "multiple_choice"),
        ("quiz3-demo.jpg", "4. Which is preterite? (a) tenía (b) tuvo", "b", 1, 1, "", "preterite_vs_imperfect", "multiple_choice"),
        ("quiz3-demo.jpg", "5. Translate: They said the truth.", "Ellos dijieron la verdad.", 1, 2, "dijeron (no i)", "irregular_preterite", "translation"),
        ("quiz3-demo.jpg", "6. Explain when to use the imperfect.", "for things that happened a lot", 2, 3, "also descriptions + background", "preterite_vs_imperfect", "short_answer"),
    ]
    return [
        TestRow(course_id=SPANISH, source=s, question=qq, student_answer=a, marks=m, marks_possible=p, teacher_note=n, topic=t, qtype=k)
        for s, qq, a, m, p, n, t, k in q
    ]


# Made-up start history for --usage: weekday (Monday 0) -> minutes late for each of the last three
# weeks, oldest first, all planned for 4 PM. Under 15 is a plain late start, under 60 is "+15 min"
# until the start, 60 or more is a missed start moved to a picked time. Tuesdays run about 25 min
# late (the Dashboard's one "you usually start late" line); one Thursday miss moves to 7 PM.
USAGE = {0: (3, 0, 5), 1: (22, 31, 25), 2: (4, 8, 2), 3: (6, 185, 9), 4: (0, 5, 12), 6: (14, 2, 6)}
USAGE_ASSIGNMENTS = (9_000_101, 9_000_102, 9_000_103)  # the graded history, so the week stays as planned


def _session(conn, assignment_id: int, planned: datetime, late: int) -> None:
    m = timedelta(minutes=1)
    length = 45 * m
    begun = planned + late * m
    ping_at = planned
    if late >= 60:
        ping_at = begun - 5 * m  # the picked time; the start comes 5 min after its ping
        miss = db.save_block(conn, Block(assignment_id=assignment_id, start=planned, end=planned + length, status="missed"))
        db.save_ping(conn, Ping(block_id=miss, sent_at=planned, answer="pick_time", picked_time=ping_at))
    block = db.save_block(conn, Block(assignment_id=assignment_id, start=begun, end=begun + length, status="done"))
    while ping_at + 15 * m <= begun:
        db.save_ping(conn, Ping(block_id=block, sent_at=ping_at, answer="plus_15"))
        ping_at += 15 * m
    db.save_ping(conn, Ping(block_id=block, sent_at=ping_at, answer="start_now", started_at=begun))


def _usage(conn, now: datetime) -> None:
    """Save USAGE's sessions, each on the latest such weekday before today and the two weeks before."""
    four = now.astimezone(local_tz()).replace(hour=16, minute=0, second=0, microsecond=0)
    n = 0
    for day, lates in USAGE.items():
        back = (four.weekday() - day) % 7 or 7
        for week, late in enumerate(lates):
            planned = four - timedelta(days=back + 7 * (len(lates) - 1 - week))
            _session(conn, USAGE_ASSIGNMENTS[n % len(USAGE_ASSIGNMENTS)], planned, late)
            n += 1


def clear(conn) -> None:
    ids = (ID_MIN, ID_MAX)
    conn.execute("DELETE FROM pings WHERE block_id IN (SELECT id FROM blocks WHERE assignment_id BETWEEN ? AND ?)", ids)
    conn.execute("DELETE FROM blocks WHERE assignment_id BETWEEN ? AND ?", ids)
    conn.execute("DELETE FROM prep_cards WHERE assignment_id BETWEEN ? AND ?", ids)
    conn.execute("DELETE FROM assignments WHERE id BETWEEN ? AND ?", ids)
    conn.execute("DELETE FROM test_rows WHERE course_id BETWEEN ? AND ?", ids)
    conn.execute("DELETE FROM courses WHERE id BETWEEN ? AND ?", ids)
    conn.commit()


def seed(conn, now: datetime | None = None, usage: bool = False) -> None:
    """Replace any existing demo data with a fresh copy dated around `now`, and plan the week.
    `usage` adds the made-up start history (USAGE)."""
    now = now or clock.now()
    clear(conn)
    for c in COURSES:
        db.save_course(conn, c)
    for a in _assignments(now):
        db.save_assignment(conn, a)
    for card in _cards():
        db.save_card(conn, card)
    db.save_test_rows(conn, _test_rows())
    if usage:
        _usage(conn, now)
    loop.replan(conn, now)


if __name__ == "__main__":
    conn = db.connect()
    db.init_db(conn)
    if "--clear" in sys.argv:
        clear(conn)
        print("Demo data removed.")
    else:
        loop.load_clock(conn)  # date the demo around the simulated clock if one is set
        usage = "--usage" in sys.argv
        seed(conn, usage=usage)
        extra = " with three made-up weeks of starts" if usage else ""
        print(f"Demo data loaded around {clock.now():%a %b %d %I:%M %p}{extra}. Start the server and open http://localhost:8000")
