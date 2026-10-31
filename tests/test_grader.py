"""Graded tests: joining pages, checking the teacher's arithmetic, course detection and types.
The model is never called: pages are built by hand (a made-up French quiz) or a fake replies."""

import pypdf
import pytest
from fastapi.testclient import TestClient

from app import db, grader, meta_client, student_model
from app.config import settings
from app.grader import GradedPage, PageItem, PageSection
from app.main import app
from app.models import Course, TestRow

FRENCH, CHEM = 3, 4


def item(n, result="unmarked", **kw) -> PageItem:
    base = dict(number=str(n), question=f"Question {n} ____", student_answer=f"answer {n}", blanks=1,
                marks_seen="", result=result, wrong_marks=0, points_lost=None, points_earned=None,
                points_possible=None, teacher_note="", topic="")
    return PageItem(**{**base, **kw})


def section(name, qtype, items, earned=None, possible=None, **kw) -> PageSection:
    base = dict(name=name, instructions=f"{name} instructions.", continues_previous=False, bonus=False,
                qtype=qtype, new_qtype_description="", topic="passe_compose", subtotal_earned=earned,
                subtotal_possible=possible, items=items)
    return PageSection(**{**base, **kw})


def page(sections, **kw) -> GradedPage:
    base = dict(course_id=FRENCH, subject="French", title="", sections=sections, page_total=None,
                score_written="", score=None, score_possible=None, bonus_added=None)
    return GradedPage(**{**base, **kw})


def quiz() -> list[GradedPage]:
    """Three pages: X marks only, deductions (one bare "0.5"), a section across two pages, a bonus."""
    bank = [item(i) for i in range(1, 11)]
    bank[1] = item(2, "incorrect", wrong_marks=1, marks_seen="X")
    bank[4] = item(5, "incorrect", wrong_marks=1, marks_seen="X")
    bank[6] = item(7, "incorrect", wrong_marks=2, marks_seen="XX")
    p1 = page([section("Verb from word bank", "word_bank", bank, 16, 20)], title="Le passé composé",
              score_written="39 + 1 = 80%", score=80, score_possible=100, bonus_added=1)
    answers = [
        item(1, "partial", points_lost=1, marks_seen="-1", teacher_note="être, pas avoir"),
        item(2, "partial", points_earned=0.5, marks_seen="0.5"),  # means 0.5 off: the section adds up that way
        item(3, "partial", points_lost=2, marks_seen="-2"),
    ]
    fixes = [item(1, "correct"), item(2, "incorrect", wrong_marks=1), item(3, "correct")]
    p2 = page([
        section("Answer in full sentences", "sentence_answer", answers, 11.5, 15),
        section("Correct the errors", "error_correction", fixes),
    ], page_total=11.5)
    p3 = page([
        section("Correct the errors", "error_correction", [item(4, "incorrect", wrong_marks=1), item(5, "correct")],
                3, 5, continues_previous=True),
        section("Translate", "translation_to_target", [item(1, "partial", points_lost=1.5), item(2)], 8.5, 10),
        section("Bonus", "fill_in_blank", [item(1, "correct"), item(2, "incorrect", wrong_marks=1)], 1, None,
                bonus=True),
    ], page_total=11.5)
    return [p1, p2, p3]


def test_quiz_adds_up():
    test, rows, used = grader.reconcile(quiz(), FRENCH, FRENCH, "quiz.pdf", grader.seed_qtypes("French II"))
    assert test.mismatches == []
    assert (test.score, test.score_possible, test.percent) == (80, 100, 80)
    assert (test.earned, test.possible, test.bonus) == (39, 50, 1)
    assert [(s.name, s.earned, s.possible, s.bonus) for s in test.sections] == [
        ("Verb from word bank", 16, 20, False),
        ("Answer in full sentences", 11.5, 15, False),
        ("Correct the errors", 3, 5, False),
        ("Translate", 8.5, 10, False),
        ("Bonus", 1, None, True),
    ]
    joined = test.sections[2]
    assert (joined.questions, joined.wrong, joined.page) == (5, 2, 3)
    assert len(rows) == 22
    main = [r for r in rows if not r.bonus]
    assert sum(r.marks for r in main) == pytest.approx(39)
    assert sum(r.marks_possible for r in main) == pytest.approx(50)
    bank = {r.question.split(".")[0]: r for r in rows if r.section == "Verb from word bank"}
    assert (bank["1"].marks, bank["1"].marks_possible, bank["1"].correct, bank["1"].estimated) == (2, 2, True, False)
    assert (bank["2"].marks, bank["2"].correct, bank["2"].estimated) == (1, False, True)  # one X of four
    assert bank["7"].marks == 0  # XX: two of the four lost points
    sentences = [r for r in rows if r.section == "Answer in full sentences"]
    assert [r.marks for r in sentences] == [4, 4.5, 3] and not any(r.estimated for r in sentences)
    assert sentences[0].teacher_note == "être, pas avoir"
    bonus = [r for r in rows if r.bonus]
    assert [(r.marks, r.marks_possible, r.correct) for r in bonus] == [(None, None, True), (None, None, False)]
    assert {r.qtype for r in rows} == {"word_bank", "sentence_answer", "error_correction", "translation_to_target",
                                       "fill_in_blank"}
    assert used["word_bank"].startswith("choose a word")


def test_saved_quiz_feeds_the_grid_without_the_bonus(conn):
    db.save_course(conn, Course(id=FRENCH, name="French II"))
    test, rows, used = grader.reconcile(quiz(), FRENCH, FRENCH, "quiz.pdf", grader.seed_qtypes("French II"))
    test = grader.save(conn, test, rows, used)
    cells = student_model.grid_cells(conn, FRENCH)
    assert sum(c["possible"] for c in cells) == pytest.approx(50)
    assert sum(c["lost"] for c in cells) == pytest.approx(11)
    assert all(r.test_id == test.id for r in db.list_test_rows(conn, FRENCH))
    assert db.get_graded_test(conn, test.id).sections[0].earned == 16


def one_page(sec, **kw):
    return grader.reconcile([page([sec], **kw)], FRENCH, FRENCH, "q.jpg", {})


def test_deductions_that_dont_match_the_subtotal_are_flagged():
    sec = section("Translate", "translation_to_target",
                  [item(1, "partial", points_lost=1), item(2, "partial", points_lost=1), item(3)], 7, 10)
    test, rows, _ = one_page(sec)
    assert test.mismatches == ["Translate: the marks at its questions take off 2, but the section shows 7/10 (3 off)"]
    assert test.needs_review
    assert sum(r.marks for r in rows) == pytest.approx(7)  # the subtotal still holds
    assert all(r.estimated for r in rows[:2]) and rows[2].correct is True


def test_points_off_with_nothing_marked_wrong_are_flagged():
    test, rows, _ = one_page(section("Vocabulary", "vocabulary", [item(1), item(2)], 8, 10))
    assert test.mismatches == ["Vocabulary: 2 points off (8/10), but no question is marked wrong"]
    assert sum(r.marks for r in rows) == pytest.approx(8)


def test_unmarked_blanks_take_the_lost_points():
    """A missed error in a correct-the-errors passage: blank, and the teacher only checked the rest."""
    fixes = [item(1, "correct"), item(2, student_answer=""), item(3, "unmarked"), item(4, student_answer="")]
    test, rows, _ = one_page(section("Correct the errors", "error_correction", fixes, 2, 4))
    assert test.mismatches == []
    assert [(r.marks, r.correct) for r in rows] == [(1, True), (0, False), (1, True), (0, False)]


def test_wrong_written_score_is_flagged():
    sec = section("Vocabulary", "vocabulary", [item(1, "incorrect"), item(2)], 5, 10)
    test, _, _ = one_page(sec, score_written="85%", score=85, score_possible=100)
    assert test.mismatches == ["The sections add up to 5/10, but the score written on the test is 85%"]
    ok, _, _ = one_page(sec, score_written="50%", score=50, score_possible=100)
    assert ok.mismatches == [] and ok.percent == 50


def test_page_total_is_checked():
    sec = section("Vocabulary", "vocabulary", [item(1, "incorrect"), item(2)], 5, 10)
    test, _, _ = one_page(sec, page_total=6)
    assert test.mismatches == ["Page 1: its sections add up to 5, but the page total says 6"]


def test_bonus_that_disagrees_with_the_score_line_is_flagged():
    secs = [section("Vocabulary", "vocabulary", [item(1)], 10, 10),
            section("Bonus", "vocabulary", [item(1, "correct")], 2, None, bonus=True)]
    test, _, _ = grader.reconcile([page(secs, score_written="10 + 3 = 13", score=13, score_possible=10,
                                        bonus_added=3)], FRENCH, FRENCH, "q.jpg", {})
    assert "Bonus: the bonus section shows 2, but the score adds 3" in test.mismatches


def test_no_points_written_counts_answers():
    sec = section("Vocabulary", "vocabulary", [item(1, "incorrect", wrong_marks=1), item(2), item(3, "partial")])
    test, rows, _ = one_page(sec)
    assert test.mismatches == []
    assert test.notes == ["Vocabulary: no points written; counted one point per answer"]
    assert [(r.marks, r.correct) for r in rows] == [(0, False), (1, True), (0.5, False)]
    assert (test.earned, test.possible, test.score) == (1.5, 3, 1.5)


def test_spread_respects_caps():
    shares, left = grader._spread(4, [1, 1, 2], [2, 2, 2])
    assert shares == pytest.approx([1, 1, 2]) and left == 0
    shares, left = grader._spread(6, [1, 1, 4], [2, 2, 2])
    assert shares == pytest.approx([2, 2, 2]) and left == 0
    _, left = grader._spread(7, [1, 1], [2, 2])
    assert left == pytest.approx(3)


# ---- course and question types ----------------------------------------------------------------


def test_course_is_detected_from_votes_or_names(conn):
    courses = [Course(id=FRENCH, name="French II"), Course(id=CHEM, name="Chemistry")]
    for c in courses:
        db.save_course(conn, c)
    assert grader.detect_course(conn, [page([]), page([], course_id=CHEM)], courses) == FRENCH
    unsure = [page([], course_id=99, subject="French", title="Le passé composé")]
    assert grader.detect_course(conn, unsure, courses) == FRENCH
    assert grader.detect_course(conn, [page([], course_id=0, subject="", title="")], courses) == 0


def test_types_are_seeded_by_subject_and_kept_per_course(conn):
    french, calc = Course(id=FRENCH, name="French II"), Course(id=9, name="AP Calculus BC")
    assert "error_correction" in grader.course_qtypes(conn, french)
    assert "setup" in grader.course_qtypes(conn, calc)
    assert grader.course_qtypes(conn, Course(id=10, name="Library")) == grader.GENERAL_QTYPES
    grader.remember_qtypes(conn, french, {"dictation": "write down sentences read aloud", "word_bank": "x"})
    kept = grader.course_qtypes(conn, french)
    assert kept["dictation"] == "write down sentences read aloud"
    assert kept["word_bank"].startswith("choose a word")  # an existing description stays


def blank_pdf(path, pages):
    w = pypdf.PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    with path.open("wb") as f:
        w.write(f)


def fake_model(monkeypatch, pages):
    prompts = []

    def reply(prompt, schema, files=None, instructions=None):
        prompts.append(prompt)
        assert schema is GradedPage and len(files) == 1 and files[0].suffix == ".pdf"
        return pages[len(prompts) - 1]

    monkeypatch.setattr(meta_client, "generate_structured", reply)
    return prompts


def test_read_test_reads_page_by_page_and_replaces_an_earlier_reading(conn, tmp_path, monkeypatch):
    db.save_course(conn, Course(id=FRENCH, name="French II"))
    db.save_course(conn, Course(id=CHEM, name="Chemistry"))
    pages = quiz()[:2]
    pages[1].sections[1].qtype = "dictation"
    pages[1].sections[1].new_qtype_description = "write down sentences read aloud"
    pdf = tmp_path / "quiz.pdf"
    blank_pdf(pdf, 2)
    db.save_test_rows(conn, [TestRow(course_id=CHEM, source="quiz.pdf", question="old reading")])
    prompts = fake_model(monkeypatch, pages)

    test, rows, used = grader.read_test(conn, pdf)
    test = grader.save(conn, test, rows, used)
    assert len(prompts) == 2
    assert "Types list" in prompts[0] and "3 French II" in prompts[0] and "4 Chemistry" in prompts[0]
    assert "This test is from the course French II" in prompts[1] and "- word_bank:" in prompts[1]
    assert 'The previous page ended in the section "Verb from word bank"' in prompts[1]
    assert (test.course_id, test.detected_course_id, test.pages) == (FRENCH, FRENCH, 2)
    assert db.list_test_rows(conn, CHEM) == []  # the old reading of the same file is gone
    assert grader.course_qtypes(conn, Course(id=FRENCH, name="French II"))["dictation"] == "write down sentences read aloud"

    prompts = fake_model(monkeypatch, quiz()[:2])
    again, rows, used = grader.read_test(conn, pdf, CHEM)  # filed where the student says
    again = grader.save(conn, again, rows, used)
    assert prompts == []  # the same file is re-checked from its cached pages
    assert (again.course_id, again.detected_course_id) == (CHEM, FRENCH)
    assert [t.id for t in db.list_graded_tests(conn)] == [again.id]
    grader.read_test(conn, pdf, fresh=True)
    assert len(prompts) == 2


# ---- API --------------------------------------------------------------------------------------


@pytest.fixture
def client(conn, monkeypatch):
    db.save_course(conn, Course(id=FRENCH, name="French II"))
    db.save_course(conn, Course(id=CHEM, name="Chemistry"))
    object.__setattr__(settings, "meta_api_key", "test-key")  # conftest puts it back
    monkeypatch.setattr(grader, "read_pages", lambda conn, path, course_id=None: quiz())
    with TestClient(app) as c:
        yield c


def test_upload_detects_the_course_and_can_move_it(client, conn):
    r = client.post("/api/tests/upload", files={"file": ("quiz.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 200
    body = r.json()
    t = body["test"]
    assert (t["course_id"], t["percent"], t["needs_review"], body["saved"]) == (FRENCH, 80, False, 22)
    dash = client.get(f"/api/dashboard?course_id={FRENCH}").json()
    assert [x["score_written"] for x in dash["tests"]] == ["39 + 1 = 80%"]
    assert sum(c["possible"] for c in dash["grid"]["cells"]) == pytest.approx(50)

    moved = client.put(f"/api/tests/{t['id']}/course", json={"course_id": CHEM}).json()
    assert moved["course_id"] == CHEM and moved["detected_course_id"] == FRENCH
    assert db.list_test_rows(conn, FRENCH) == [] and len(db.list_test_rows(conn, CHEM)) == 22
    assert client.put(f"/api/tests/{t['id']}/course", json={"course_id": 77}).status_code == 404

    assert client.delete(f"/api/tests/{t['id']}").json() == {"ok": True}
    assert db.list_test_rows(conn, CHEM) == [] and db.list_graded_tests(conn) == []


def test_upload_to_an_unknown_course_is_refused(client):
    r = client.post("/api/tests/upload", data={"course_id": 77}, files={"file": ("q.jpg", b"\xff\xd8", "image/jpeg")})
    assert r.status_code == 404
