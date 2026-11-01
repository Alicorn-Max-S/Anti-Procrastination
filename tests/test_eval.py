"""The eval harness's offline parts: replaying lines from calibrate records, the proposal rules,
matching grader rows to labeled questions, and reading the multi-question replies. No model calls."""

import pytest

from decider.prompts import REPLY, choice_for
from decisions.fields import ASSIGNMENT_FIELDS, QUESTION_FIELDS
from eval import calibrate as cal
from eval import compare
from eval.items import align, page_rows

TYPE, TEMPLATE, EFFORT = ASSIGNMENT_FIELDS
FORMAT, TOPIC, CREDIT = QUESTION_FIELDS


def probe(conf: float, top: int = 0, share: float = 0.99) -> dict:
    return {"conf": conf, "top": top, "share": share, "probs": [], "probe_tokens": 0}


def rec(label: int, fwd: dict, rev: dict | None = None, trace: list[tuple[int, dict]] | None = None,
        finished: bool = True) -> dict:
    trace = trace or [(40, fwd)]
    return {
        "options": ["a", "b", "c"], "label": "abc"[label], "forward": fwd, "reversed": rev or fwd,
        "trace": [{"think_tokens": n, **p} for n, p in trace], "finished": finished,
        "think_tokens": trace[-1][0],
    }


# ---- replaying lines ------------------------------------------------------------------------


def test_gate_needs_the_line_the_share_and_the_reversed_probe():
    r = rec(0, probe(0.95), probe(0.9))
    assert cal.passes_gate(r, 0.9, 0.5)
    assert not cal.passes_gate(r, 0.96, 0.5)
    assert not cal.passes_gate(rec(0, probe(0.95, share=0.4)), 0.9, 0.5)
    flipped = rec(0, probe(0.95), probe(0.9, top=1))
    assert not cal.passes_gate(flipped, 0.9, 0.5)
    assert cal.passes_gate(flipped, 0.9, 0.5, reversed_check=False)


def test_loop_stops_at_the_first_probe_that_clears():
    r = rec(1, probe(0.5), trace=[(30, probe(0.7, 0)), (70, probe(0.92, 1)), (120, probe(0.99, 1))])
    assert cal.loop_end(r, 0.9, 0.5) == cal.Outcome("thought", 1, True, False, 70)
    assert cal.loop_end(r, 0.95, 0.5).think_tokens == 120
    capped = rec(1, probe(0.5), trace=[(300, probe(0.6, 0)), (530, probe(0.7, 1))], finished=False)
    assert cal.loop_end(capped, 0.9, 0.5) == cal.Outcome("guessed", 1, True, True, 530)
    closed = rec(1, probe(0.5), trace=[(80, probe(0.6, 0))], finished=True)
    assert not cal.loop_end(closed, 0.9, 0.5).cap_hit


def test_decide_skips_thinking_for_skip_fields():
    r = rec(0, probe(0.6, 2), trace=[(50, probe(0.99, 0))])
    assert cal.decide(r, 0.9, 0.9, False, 0.5).tier == "thought"
    assert cal.decide(r, 0.9, 0.9, True, 0.5) == cal.Outcome("guessed", 2, False, False, 0)
    assert cal.decide(r, 0.5, 0.9, True, 0.5).tier == "instant"


# ---- the proposal rules ---------------------------------------------------------------------


def test_expensive_line_is_the_lowest_with_zero_errors():
    recs = [rec(0, probe(0.99)), rec(0, probe(0.97)), rec(1, probe(0.96, 0)),  # wrong at 0.96
            rec(0, probe(0.93)), rec(0, probe(0.995))]
    p = cal.plan_field("type", recs, 0.5)
    assert 0.96 < p.fast <= 0.97
    assert not [r for r in recs if cal.passes_gate(r, p.fast, 0.5) and r["forward"]["top"] != cal.right(r)]
    assert p.fast_error == pytest.approx(0.2)
    assert not p.skip  # expensive fields never skip thinking


def test_the_reversed_check_can_lower_the_line():
    wrong_but_flips = rec(1, probe(0.99, 0), probe(0.8, 1))
    recs = [wrong_but_flips, rec(0, probe(0.9)), rec(0, probe(0.95))]
    p = cal.plan_field("template", recs, 0.5)
    assert p.fast <= 0.9  # the reversed probe catches the error, so nothing else has to
    assert p.fast_forward_only == cal.NEVER  # forward alone: the error has the top confidence


def test_cheap_line_allows_ten_percent_and_skip_needs_ten_percent_fast_error():
    recs = [rec(0, probe(0.6 + i / 100)) for i in range(18)] + [rec(1, probe(0.65, 0)), rec(1, probe(0.62, 0))]
    p = cal.plan_field("effort", recs, 0.5)
    assert p.fast == 0.6  # 2 wrong of 20 accepted is 10%
    assert p.skip and p.fast_error == pytest.approx(0.1)
    worse = recs + [rec(2, probe(0.61, 0))]
    assert not cal.plan_field("effort", worse, 0.5).skip


def test_no_zero_error_line_means_never():
    p = cal.plan_field("credit", [rec(1, probe(1.0, 0)), rec(0, probe(0.9))], 0.5)
    assert p.fast == cal.NEVER


def test_think_line_is_replayed_on_the_fields_left_unsure():
    recs = [
        rec(0, probe(0.99)),  # instant
        rec(0, probe(0.5), trace=[(40, probe(0.8, 0)), (90, probe(0.97, 0))]),
        rec(1, probe(0.5, 0), trace=[(40, probe(0.9, 0)), (90, probe(0.98, 1))]),  # wrong at 0.9
    ]
    p = cal.plan_field("type", recs, 0.5)
    assert p.fast <= 0.99 and p.think_data
    assert 0.9 < p.think <= 0.97
    outs = [cal.decide(r, p.fast, p.think, False, 0.5) for r in recs]
    assert all(o.top == cal.right(r) for o, r in zip(outs, recs))


def test_tidy_keeps_outcomes():
    confs = [0.9734, 0.9701, 0.99]
    outcome = lambda x: frozenset(c for c in confs if c >= x)  # noqa: E731
    assert cal.tidy(0.9734, outcome) == 0.973  # 0.97 would let 0.9701 in too
    assert cal.tidy(0.99, outcome) == 0.99
    assert cal.tidy(cal.NEVER, outcome) == cal.NEVER


def test_histogram_puts_one_in_the_top_bin():
    out = cal.histogram([(1.0, True), (0.42, False)])
    assert out[-1].strip().startswith(">=0.999") and out[-1].rstrip().endswith("0")
    assert "1" in out[-1] and out[0].rstrip().endswith("1")


# ---- test rows ------------------------------------------------------------------------------


def test_align_by_order_then_by_number():
    rows = [{"number": str(k)} for k in (1, 2, 3)]
    assert align(rows, 3)[0] == rows
    extra = rows + [{"number": "bonus"}]
    assert align(extra, 3)[0] == rows
    restart = [{"number": "1"}, {"number": "2"}, {"number": "1"}, {"number": "2"}]
    got, why = align(restart, 3)
    assert got is None and "Spark read 4 questions where 3 are labeled" in why


def test_page_rows_copy_marks_literally_and_carry_sections():
    from app.grader import GradedPage, PageItem, PageSection

    def item(n, marks):
        return PageItem(number=n, question="Mi  ____ coche", student_answer="su", blanks=1, marks_seen=marks,
                        result="incorrect", wrong_marks=1, points_lost=None, points_earned=None,
                        points_possible=2, teacher_note=" mi ", topic="")

    def section(instr, cont, items):
        return PageSection(name="Possessives", instructions=instr, continues_previous=cont, bonus=False,
                           qtype="fill_in_blank", new_qtype_description="", topic="", subtotal_earned=None,
                           subtotal_possible=None, items=items)

    def page(sections):
        return GradedPage(course_id=1, subject="", title="", sections=sections, page_total=None,
                          score_written="", score=None, score_possible=None, bonus_added=None)

    rows = page_rows([page([section("Fill in the possessive.", False, [item("1", "X")])]),
                      page([section("", True, [item("2", "-1")])])])
    assert rows[0] == {"number": "1", "text": "1. Mi ____ coche", "answer": "su", "marks": "X",
                       "note": "mi", "points": 2, "section": "Fill in the possessive.", "bonus": False}
    assert rows[1]["section"] == "Fill in the possessive." and rows[1]["marks"] == "-1"


def test_written_marks_add_points_and_a_lone_questions_section_score():
    from app.grader import PageItem, PageSection

    def item(marks, lost=None, earned=None, possible=None):
        return PageItem(number="1", question="q", student_answer="a", blanks=1, marks_seen=marks,
                        result="partial", wrong_marks=0, points_lost=lost, points_earned=earned,
                        points_possible=possible, teacher_note="", topic="")

    def section(items, earned=None, possible=None):
        return PageSection(name="n", instructions="i", continues_previous=False, bonus=False, qtype="t",
                           new_qtype_description="", topic="", subtotal_earned=earned,
                           subtotal_possible=possible, items=items)

    from eval.items import written_marks

    it = item("", lost=1.5, earned=2.5, possible=4)
    assert written_marks(it, section([it, item("")])) == "2.5/4"
    it = item("X", lost=1)
    assert written_marks(it, section([it, item("")], 8, 10)) == "X, -1"  # the subtotal is the section's
    it = item("", lost=0)
    assert written_marks(it, section([it], 2, 3)) == "2/3"
    it = item("-1", lost=1)
    assert written_marks(it, section([it, item("")])) == "-1"


# ---- the multi-question setups --------------------------------------------------------------


def test_questions_are_the_deciders_blocks():
    cs = [choice_for(f) for f in ASSIGNMENT_FIELDS]
    text = compare.questions_text(cs)
    for c in cs:
        assert c.block().removesuffix("\n" + REPLY) in text
    assert REPLY not in text
    assert "and a few multiple-choice questions" in compare.MULTI_SYSTEM
    assert "Reasoning strength" not in compare.SPARK_INSTRUCTIONS


def test_parse_letters():
    cs = [choice_for(f) for f in ASSIGNMENT_FIELDS]
    got = compare.parse_letters("type: D\n**template**: (B)\neffort - Z", cs)
    assert got == {"type": "problem_set_or_worksheet", "template": "checklist", "effort": None}
    assert compare.parse_letters("I think A", cs)["type"] is None


def test_topic_letters_follow_the_class_list():
    c = choice_for(TOPIC, ["por_y_para", "comparaciones"])
    assert compare.letter_value(c, "B") == "comparaciones"
    assert compare.letter_value(c, "C") == "none_fit"
    assert compare.letter_value(c, "D") is None


def test_spark_cost_uses_cached_price():
    usage = {"input_tokens": 1_000_000, "input_tokens_details": {"cached_tokens": 400_000},
             "output_tokens": 100_000}
    assert compare.spark_cost(usage) == pytest.approx(0.6 * 1.25 + 0.4 * 0.15 + 0.1 * 4.25)


def test_score_counts_tiers_caps_and_fast_answers():
    from eval.items import Item

    item = Item("assignment", "assignment:1", "Bio", {}, {"type": "reading", "effort": "multi_day"})
    r = {"answers": {"type": "reading", "template": "document", "effort": "one_short_sitting"},
         "fast_answers": {"type": "lab_report", "template": "document", "effort": "multi_day"},
         "tiers": {"type": "thought", "template": "instant", "effort": "guessed"},
         "looped": {"type": True, "template": False, "effort": True},
         "cap_hits": {"type": False, "template": False, "effort": True},
         "think_tokens": {"type": 60, "template": 0, "effort": 540},
         "seconds": 4.0, "fast_seconds": 0.5}
    full = compare.score("system_one", [item], {item.key: r})
    assert full.accuracy(["type", "effort"]) == (1, 2)
    assert full.tiers["effort"]["guessed"] == 1 and full.cap_hits["effort"] == 1
    fast = compare.score("fast", [item], {item.key: r})
    assert fast.accuracy(["type", "effort"]) == (1, 2) and fast.sec_per_item == 0.5
    assert fast.cost_per_1000 == pytest.approx(0.5 * 1000 / 3600 * compare.GPU_HOUR)


def test_combine_blank_labels():
    from eval.items import combine

    blanks = [{"format": "fill_in_the_blank", "credit": "none"}, {"format": "fill_in_the_blank", "credit": "full"}]
    assert combine(blanks) == ({"format": "fill_in_the_blank", "credit": "partial"}, {})
    assert combine([{"credit": "full"}, {"credit": "full"}]) == ({"credit": "full"}, {})
    assert combine([{"topic": "a"}, {"topic": "b"}]) == ({}, {"topic": "a/b"})


def test_corrections_fix_one_question_and_refuse_ambiguous_ones():
    import pytest

    from eval.items import corrected

    pages = [{"sections": [{"name": "C", "items": [{"number": "4", "marks_seen": "X, -1"},
                                                   {"number": "5", "marks_seen": ""}]}]}]
    fix = {"page": 1, "section": "C", "number": "4", "set": {"marks_seen": "X, -1, +1"}, "why": "photo"}
    out = corrected(pages, [fix])
    assert out[0]["sections"][0]["items"][0]["marks_seen"] == "X, -1, +1"
    assert pages[0]["sections"][0]["items"][0]["marks_seen"] == "X, -1"  # Spark's reading stays as read
    with pytest.raises(ValueError):
        corrected(pages, [{**fix, "number": "9"}])


def test_options_show_their_definitions():
    from decider.prompts import choice_for
    from decisions.fields import QUESTION_FIELDS

    block = choice_for(next(f for f in QUESTION_FIELDS if f.key == "credit")).block()
    assert "A) Full: no points lost." in block and block.endswith("Reply with just the letter.")


def test_examples_are_valid_and_in_the_shared_system_turn():
    from decider.prompts import SYSTEM
    from decisions.fields import ASSIGNMENT_FIELDS, QUESTION_FIELDS, field_values

    for f in ASSIGNMENT_FIELDS + QUESTION_FIELDS:
        for e in f.examples:
            assert e.answer in field_values(f), (f.key, e)
            assert e.item in SYSTEM
            assert not e.source or e.source.startswith(("assignment:", "test_question:"))
        if f.rules:  # under the field's question in order, ahead of its examples
            assert f.question + "".join(f"\nRule: {r}" for r in f.rules) in SYSTEM, f.key
    assert SYSTEM.endswith("\n\nReasoning strength: low.")


def test_an_examples_source_label_is_held_out(monkeypatch):
    from eval import items as eval_items

    monkeypatch.setattr(eval_items, "held_out", lambda: {("assignment:1", "type")})
    skipped = []
    keep = eval_items._valid("assignment:1", eval_items.ASSIGNMENT_FIELDS,
                             {"type": "reading", "effort": "multi_day"}, [], skipped)
    assert keep == {"effort": "multi_day"}
    assert [(s.field, s.reason) for s in skipped] == [("type", "used as a prompt example")]
