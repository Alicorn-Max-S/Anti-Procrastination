"""The Spark vs Glimmer race (eval/race.py) and the diagram and video that read its log
(eval/race_media.py): the scoring and the replay's clock, with no model calls."""

from eval import race, race_media


def row(n: int, start: float, end: float, right: dict[str, bool], **extra) -> dict:
    return {"n": n, "key": f"q{n}", "number": str(n), "start": start, "end": end, "seconds": end - start,
            "answers": {}, "labels": {}, "right": right, **extra}


SPARK = [
    row(1, 0.0, 8.0, {"format": True, "topic": True, "credit": True}, cost=0.01, usage={"input_tokens": 100, "output_tokens": 40}),
    row(2, 8.0, 20.0, {"format": True, "topic": False, "credit": True}, cost=0.02, usage={"input_tokens": 120, "output_tokens": 60}),
]
GLIMMER = [
    row(1, 0.0, 1.0, {"format": True, "topic": True, "credit": True}, tiers={"format": "instant", "topic": "instant", "credit": "guessed"}),
    row(2, 1.0, 2.0, {"format": True, "topic": True, "credit": False}, tiers={"format": "instant", "topic": "instant", "credit": "instant"}),
]


def test_summarize_spark_counts_labels_and_adds_up_the_api_cost():
    s = race.summarize("spark", SPARK)
    assert (s["questions"], s["labeled_fields"], s["right"]) == (2, 6, 5)
    assert s["accuracy"] == round(5 / 6, 4)
    assert s["wall_seconds"] == 20.0 and s["mean_seconds"] == 10.0
    assert s["cost"] == 0.03 and (s["input_tokens"], s["output_tokens"]) == (220, 100)
    assert s["by_field"]["topic"] == {"right": 1, "of": 2}


def test_summarize_glimmer_prices_gpu_seconds_and_counts_tiers():
    s = race.summarize("glimmer", GLIMMER)
    assert s["wall_seconds"] == 2.0
    assert s["cost"] == round(2.0 / 3600 * race.GPU_HOUR, 4)
    assert s["tiers"] == {"instant": 5, "thought": 0, "guessed": 1, "default": 0}


def test_a_field_the_labeler_skipped_is_not_scored():
    only = row(1, 0.0, 1.0, {"format": True})  # no topic or credit label for this question
    s = race.summarize("glimmer", [only])
    assert (s["labeled_fields"], s["right"]) == (1, 1)
    assert "topic" not in s["by_field"]


def test_the_replay_counts_a_question_only_once_it_has_finished():
    lane = race_media.Lane("spark", "Spark", "", SPARK, race.summarize("spark", SPARK))
    assert [r["n"] for r in lane.done(7.9)] == []
    assert [r["n"] for r in lane.done(8.0)] == [1]
    assert lane.cost(8.0) == 0.01 and lane.cost(100) == 0.03


def test_the_replay_prices_glimmer_by_the_clock_and_stops_at_its_finish():
    lane = race_media.Lane("glimmer", "Glimmer", "", GLIMMER, race.summarize("glimmer", GLIMMER))
    assert lane.cost(1.0) == 1.0 / 3600 * race_media.GPU_HOUR
    assert lane.cost(500.0) == lane.cost(2.0)


def test_clock_and_span_read_like_a_stopwatch():
    assert race_media.clock(29.4) == "0:29.4"
    assert race_media.clock(272.5) == "4:32.5"
    assert race_media.span(29.38) == "29 s"
    assert race_media.span(272.53) == "4 min 33 s"
    assert race_media.pretty("double_object_pronouns") == "double object pronouns"
