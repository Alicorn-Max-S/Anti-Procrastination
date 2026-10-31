"""The weakness grid, evidence list and watch-out line, from fabricated rows."""

from app import db, student_model
from app.models import Assignment, RubricScore, SubmissionComment, TestRow


def rows(course_id=1):
    def r(topic, qtype, marks, possible, note=""):
        return TestRow(course_id=course_id, source="q.jpg", question=f"{topic}?", student_answer="x",
                       marks=marks, marks_possible=possible, teacher_note=note, topic=topic, qtype=qtype)

    return [
        r("pret_imp", "fill_in_blank", 0, 2, "habitual past"),
        r("pret_imp", "fill_in_blank", 1, 2),
        r("vocab", "multiple_choice", 1, 1),
        r("irregular", "translation", 1, 2),
        TestRow(course_id=course_id, source="q.jpg", question="unscored", topic="vocab", qtype="essay"),
    ]


def test_grid_and_weakest(conn):
    db.save_test_rows(conn, rows())
    grid = student_model.weakness_grid(conn, 1)
    assert grid[("pret_imp", "fill_in_blank")] == 0.75
    assert grid[("vocab", "multiple_choice")] == 0
    assert ("vocab", "essay") not in grid  # no marks -> no evidence

    weakest = student_model.weakest_cells(conn, 1, n=2)
    assert [(c["topic"], c["qtype"]) for c in weakest] == [("pret_imp", "fill_in_blank"), ("irregular", "translation")]
    assert weakest[0]["examples"][0]["teacher_note"] == "habitual past"


def test_watch_out_prefers_rubric_losses_of_the_same_kind(conn):
    db.save_assignment(
        conn,
        Assignment(
            id=1, course_id=1, name="Lab 3", kind="lab",
            rubric=[RubricScore(assignment_id=1, criterion="Observations", points=2, points_possible=5, comment="More detail")],
        ),
    )
    db.save_assignment(
        conn,
        Assignment(id=2, course_id=1, name="Essay 1", kind="essay",
                   comments=[SubmissionComment(id=1, assignment_id=2, author="Ms. R", text="Cite more")]),
    )
    line = student_model.watch_out_line(conn, 1, "lab")
    assert "Lab 3" in line and "3/5" in line and "Observations" in line and "More detail" in line
    assert "Ms. R" in student_model.watch_out_line(conn, 1, "essay")


def test_watch_out_falls_back_to_tests_then_empty(conn):
    assert student_model.watch_out_line(conn, 1, "lab") == ""
    db.save_test_rows(conn, rows())
    assert "pret_imp" in student_model.watch_out_line(conn, 1, "test")


def test_pointer_comments_are_not_evidence(conn):
    pointer = (
        'Max, you can find the scored rubric and the feedback on the shared Google doc "Unit 1 Assessment - Max." '
        "Please schedule a conference with me if you'd like to discuss the feedback."
    )
    assert not student_model.is_feedback(pointer)
    assert not student_model.is_feedback("OG: 78.6%")
    assert not student_model.is_feedback("See https://example.com/notes")
    assert not student_model.is_feedback("  ")
    assert student_model.is_feedback("Cite more")
    assert student_model.is_feedback("Good evidence, but explain why the federal negative mattered.")

    db.save_assignment(
        conn,
        Assignment(
            id=3, course_id=1, name="Essay 2", kind="essay",
            comments=[
                SubmissionComment(id=1, assignment_id=3, author="Ms. R", text=pointer),
                SubmissionComment(id=2, assignment_id=3, author="Ms. R", text="Cite more"),
            ],
            rubric=[RubricScore(assignment_id=3, criterion="Evidence", points=2, points_possible=4, comment=pointer)],
        ),
    )
    items = student_model.feedback_evidence(conn, 1)
    assert [i["text"] for i in items if i["source"] == "comment"] == ["Cite more"]
    assert [i["teacher_comment"] for i in items if i["source"] == "rubric"] == [""]


def test_rubric_losses_are_rounded(conn):
    db.save_assignment(
        conn,
        Assignment(
            id=4, course_id=1, name="Quick write #1", kind="essay",
            rubric=[
                RubricScore(assignment_id=4, criterion="Assertion", points=2.85, points_possible=3),
                RubricScore(assignment_id=4, criterion="Connections", points=3.75, points_possible=4),
                RubricScore(assignment_id=4, criterion="Clarity", points=0.3 + 0.6, points_possible=0.9),  # no real loss
            ],
        ),
    )
    items = {i["criterion"]: i for i in student_model.feedback_evidence(conn, 1)}
    assert items["Assertion"]["points_lost"] == 0.15
    assert items["Connections"]["points_lost"] == 0.25
    assert "Clarity" not in items
    assert "lost 0.25/4" in student_model.watch_out_line(conn, 1, "essay")  # worst share of marks first


def test_evidence_is_per_course(conn):
    db.save_test_rows(conn, rows(course_id=2))
    assert student_model.feedback_evidence(conn, 1) == []
    assert student_model.feedback_evidence(conn, 2)[0]["source"] == "graded_test"
