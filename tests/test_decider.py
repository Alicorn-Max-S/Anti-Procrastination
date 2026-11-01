"""The decider against a fake llama-server: letter parsing, the gate, the thinking loop's cap and
<|eom|> handling, topic naming and the HTTP API. Nothing here talks to a real llama-server.

The fake tokenizes one character per token (special tokens are one token each), keeps one slot's
prompt cache so `prompt_n` behaves like the real server's, and answers probes from a script that
names options by label, so a test doesn't care which letter an option got.
"""

import json
import math
import re
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from decider import main
from decider.config import Settings
from decider.decide import Decider
from decider.gate import fast_pass
from decider.llama import Llama
from decider.probe import letter_probs, read_probe
from decider.prompts import (
    ANSWER,
    DESCRIPTION_CHARS,
    SYSTEM,
    THINK_CLOSE,
    THINK_OPEN,
    Prompt,
    assignment_context,
    choice_for,
)
from decider.think import think
from decisions.fields import ASSIGNMENT_FIELDS, QUESTION_FIELDS

TYPE, TEMPLATE, EFFORT = ASSIGNMENT_FIELDS
FORMAT, TOPIC, CREDIT = QUESTION_FIELDS

SPECIAL = {"<|begin_of_text|>": 200000, "<|eom|>": 200007, "<|eot|>": 200008,
           "<|start|>": 200022, "<|message|>": 200023}
ID_SPECIAL = {v: k for k, v in SPECIAL.items()}
SPLIT = re.compile("(" + "|".join(map(re.escape, SPECIAL)) + ")")
EOM, EOT = SPECIAL["<|eom|>"], SPECIAL["<|eot|>"]


def tok(text: str) -> list[int]:
    ids = []
    for part in SPLIT.split(text):
        ids += [SPECIAL[part]] if part in SPECIAL else [ord(c) for c in part]
    return ids


def detok(ids: list[int]) -> str:
    return "".join(ID_SPECIAL.get(i, chr(i)) for i in ids)


def words(n: int, end: str = "") -> list[int]:
    """n tokens of thinking (one character each), the last ones spelling `end`."""
    return tok("x" * (n - len(end)) + end)


OPEN_IDS = tok(THINK_OPEN)


class Req:
    """One /completion request as the fake sees it."""

    def __init__(self, body: dict) -> None:
        self.body = body
        self.ids = body["prompt"]
        self.text = detok(self.ids)
        block = self.text[self.text.rindex("Question: "):]
        self.question = block.split("\n", 1)[0].removeprefix("Question: ")
        self.options = dict(re.findall(r"^([A-Z])\) (.+)$", block, re.M))  # letter -> label
        self.think_n = self._thinking()

    def _thinking(self) -> int:
        """Thinking tokens in this prompt (after the last open thinking turn, up to <|eom|>)."""
        n = len(OPEN_IDS)
        starts = [i for i in range(len(self.ids) - n + 1) if self.ids[i:i + n] == OPEN_IDS]
        if not starts:
            return 0
        rest = self.ids[starts[-1] + n:]
        return rest.index(EOM) if EOM in rest else len(rest)


class FakeLlama:
    def __init__(self, probe=None, think=None, name=None) -> None:
        self.probe_fn = probe or (lambda r: {})
        self.think_fn = think or (lambda r: (words(10) + [EOM], "eos"))
        self.name_fn = name or (lambda r: "")
        self.requests: list[tuple[str, Req, dict]] = []  # (kind, request, timings)
        self.cache: list[int] = []

    def client(self, settings: Settings) -> Llama:
        return Llama(settings.glimmer_url, httpx.Client(transport=httpx.MockTransport(self.handle)))

    def kinds(self, kind: str) -> list[Req]:
        return [r for k, r, _ in self.requests if k == kind]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/props":
            return httpx.Response(200, json={"build_info": "b12023-fake",
                                             "model_alias": "models/Muse-Glimmer-fake.gguf"})
        body = json.loads(request.content)
        if path == "/tokenize":
            ids = tok(body["content"])
            return httpx.Response(200, json={"tokens": [200000] * bool(body.get("add_special")) + ids})
        if path == "/detokenize":
            return httpx.Response(200, json={"content": detok(body["tokens"])})
        if path == "/apply-template":
            turns = "".join(f"<|start|>{m['role']}<|message|>{m['content']}<|eot|>" for m in body["messages"])
            return httpx.Response(200, json={"prompt": "<|begin_of_text|>" + turns})
        assert path == "/completion", path
        return httpx.Response(200, json=self.complete(body))

    def complete(self, body: dict) -> dict:
        prompt = body["prompt"]
        assert prompt[0] == 200000 and prompt.count(200000) == 1, "exactly one BOS"
        same = 0
        while same < min(len(self.cache), len(prompt)) and self.cache[same] == prompt[same]:
            same += 1
        same = min(same, len(prompt) - 1)  # the last prompt token is always processed
        timings = {"prompt_n": len(prompt) - same, "cache_n": same}
        req = Req(body)
        if "n_probs" in body:
            kind = "probe"
            by_label = self.probe_fn(req)
            letter = {label: l for l, label in req.options.items()}
            tops = [{"token": letter[label], "logprob": math.log(p)} for label, p in by_label.items() if p > 0]
            resp = {"completion_probabilities": [{"top_logprobs": tops}]}
            self.cache = list(prompt)
        elif body.get("return_tokens"):
            kind = "think"
            ids, stop = self.think_fn(req)
            resp = {"tokens": ids, "stop_type": stop}
            self.cache = prompt + ids
        else:
            kind = "name"
            resp = {"content": self.name_fn(req), "stop_type": "word"}
            self.cache = list(prompt)
        self.requests.append((kind, req, timings))
        return {**resp, "timings": timings}


@pytest.fixture
def s(tmp_path) -> Settings:
    return Settings(glimmer_url="http://llama.test", parallel_slots=1, cap_tokens=512,
                    paragraph_max=96, min_probe_chunk=24, letter_share_min=0.5,
                    lines_file=str(tmp_path / "lines.json"), prompt_version="t1",
                    fast_pass_format="empty-thinking")


def sure(label: str, p: float = 0.97, other: str | None = None) -> dict:
    return {label: p, **({other: 1 - p - 0.01} if other else {})}


ASSIGNMENT = {
    "course": "Biology", "title": "Chapter 5 reading", "due": "Mon Nov 2, 2026, 11:59 PM",
    "points": 10, "submission_types": ["online_text_entry"], "attachments": ["ch5.pdf"],
    "rubric": [{"criterion": "Notes", "points": 6}, {"criterion": "Summary", "points": 4}],
    "description": "Read chapter 5 and take notes.",
}


# ---- 1, 2: letters, confidence and letter_share ------------------------------------------------


def test_letter_probs_count_bare_and_spaced_letters():
    tops = [
        {"token": "A", "logprob": math.log(0.5)},
        {"token": " A", "logprob": math.log(0.2)},
        {"token": " B", "logprob": math.log(0.1)},
        {"token": "C", "prob": 0.05},  # older builds send prob instead of logprob
        {"token": "  A", "logprob": math.log(0.01)},  # two spaces: not a letter
        {"token": "D", "logprob": math.log(0.04)},  # past the last option
        {"token": "The", "logprob": math.log(0.1)},
    ]
    assert letter_probs(tops, 3) == pytest.approx([0.7, 0.1, 0.05])


def test_confidence_is_top_share_of_letter_probability():
    resp = {"completion_probabilities": [{"top_logprobs": [
        {"token": "A", "logprob": math.log(0.6)},
        {"token": " B", "logprob": math.log(0.2)},
        {"token": "Okay", "logprob": math.log(0.2)},
    ]}], "timings": {"prompt_n": 42}}
    choice = choice_for(CREDIT)
    p = read_probe(resp, choice)
    assert p.letter_share == pytest.approx(0.8)
    assert p.confidence == pytest.approx(0.75)
    assert p.top == 0 and p.probe_tokens == 42

    back = read_probe(resp, choice.reversed())  # letter A is now the last option, "Can't tell"
    assert back.probs == pytest.approx((0.0, 0.0, 0.2, 0.6))
    assert back.top == 3 and back.confidence == pytest.approx(0.75)

    empty = read_probe({"completion_probabilities": [{"top_logprobs": []}]}, choice)
    assert empty.letter_share == 0 and empty.confidence == 0


def test_reversed_topic_keeps_none_fit_last():
    choice = choice_for(TOPIC, ["a", "b", "c"]).reversed()
    assert [choice.options[i].value for i in choice.order] == ["c", "b", "a", "none_fit"]


# ---- 3: the gate --------------------------------------------------------------------------------


def run_fast(fake: FakeLlama, s: Settings, field=TYPE, line: float = 0.85):
    llama = fake.client(s)
    return fast_pass(llama, assignment_context(ASSIGNMENT), choice_for(field), line, 0.5,
                     s.fast_pass_format)


def test_no_reversed_probe_when_forward_probe_fails(s):
    fake = FakeLlama(probe=lambda r: {"Reading": 0.6, "Lab report": 0.35})  # confidence 0.63
    fp = run_fast(fake, s)
    assert len(fake.kinds("probe")) == 1
    assert fp.reversed is None and fp.flipped is None and not fp.instant


def test_no_reversed_probe_when_letter_share_is_low(s):
    fake = FakeLlama(probe=lambda r: {"Reading": 0.45})  # confident, but letters hold only 0.45
    fp = run_fast(fake, s)
    assert len(fake.kinds("probe")) == 1 and fp.reversed is None and not fp.instant


def test_reversed_probe_runs_when_forward_passes_and_must_agree(s):
    fake = FakeLlama(probe=lambda r: sure("Reading"))
    fp = run_fast(fake, s)
    forward, back = fake.kinds("probe")
    assert forward.options["A"] == "Test or quiz" and back.options["A"] == "Something else"
    assert fp.instant and fp.flipped is False

    biased = FakeLlama(probe=lambda r: sure(r.options["A"]))  # always says A, whatever A is
    fp = run_fast(biased, s)
    assert len(biased.kinds("probe")) == 2 and fp.flipped is True and not fp.instant


def test_reversed_probe_needs_letter_share_too(s):
    calls = iter([sure("Reading"), {"Reading": 0.4}])
    fake = FakeLlama(probe=lambda r: next(calls))
    fp = run_fast(fake, s)
    assert fp.flipped is False and not fp.instant


def test_fast_pass_prompt_formats(s):
    fake = FakeLlama(probe=lambda r: {"Reading": 0.6, "Lab report": 0.35})
    run_fast(fake, s)
    run_fast(fake, replace(s, fast_pass_format="no-thinking"))
    empty, none = (r.text for r in fake.kinds("probe"))
    assert empty.endswith("<|eot|>" + THINK_OPEN + THINK_CLOSE + ANSWER)
    assert none.endswith("<|eot|>" + ANSWER)


# ---- 4, 5: the thinking loop --------------------------------------------------------------------


def run_think(fake: FakeLlama, s: Settings, line: float = 0.85):
    llama = fake.client(s)
    choice = choice_for(TYPE)
    prompt = Prompt(llama, assignment_context(ASSIGNMENT), choice.block(), s.fast_pass_format)
    return think(llama, prompt, choice, line, s)


UNSURE = {"Reading": 0.5, "Lab report": 0.45}


def test_cap_no_new_paragraph_at_512_tokens(s):
    fake = FakeLlama(probe=lambda r: UNSURE, think=lambda r: (words(90), "limit"))
    t = run_think(fake, s)
    starts = [r.think_n for r in fake.kinds("think")]
    assert starts == [0, 90, 180, 270, 360, 450]  # 450 < 512 starts one more; 540 does not
    assert [n for n, _ in t.trace] == [90, 180, 270, 360, 450, 540]
    assert len(t.tokens) == 540 and not t.clear and not t.finished


def test_cap_paragraph_in_progress_finishes(s):
    chunks = iter([(words(96), "limit")] * 5 + [
        (words(30, "\n\n"), "word"),  # 510: a whole paragraph, probed
        (words(10, "\n\n"), "word"),  # 520: a new paragraph (started under the cap) is too short...
        (words(30, "\n\n"), "word"),  # ...so it goes on past 512 until it can be probed
    ])
    fake = FakeLlama(probe=lambda r: UNSURE, think=lambda r: next(chunks))
    t = run_think(fake, s)
    assert [r.think_n for r in fake.kinds("think")] == [0, 96, 192, 288, 384, 480, 510, 520]
    assert [n for n, _ in t.trace] == [96, 192, 288, 384, 480, 510, 550]
    assert len(fake.kinds("probe")) == 7


def test_short_chunk_gets_another_before_probing(s):
    chunks = iter([(words(8, "\n\n"), "word"), (words(9, "\n\n"), "word"), (words(40, "\n\n"), "word")])
    fake = FakeLlama(probe=lambda r: sure("Reading") if r.think_n >= 57 else UNSURE,
                     think=lambda r: next(chunks))
    t = run_think(fake, s)
    assert [n for n, _ in t.trace] == [57]  # 8 and 17 were too short to probe
    assert [r.think_n for r in fake.kinds("think")] == [0, 8, 17]
    assert t.clear and len(t.tokens) == 57


def test_stops_on_confidence_not_agreement(s):
    # The same top option every probe, but under the line: it keeps thinking until it clears.
    fake = FakeLlama(probe=lambda r: sure("Reading") if r.think_n >= 120 else UNSURE,
                     think=lambda r: (words(40, "\n\n"), "word"))
    t = run_think(fake, s)
    assert [n for n, _ in t.trace] == [40, 80, 120] and t.clear


def test_eom_ends_the_loop_and_is_cut(s):
    answer_after = tok("<|start|>assistant to=user<|message|>B") + [EOT]
    chunks = iter([(words(40, "\n\n"), "word"), (words(10) + [EOM] + answer_after, "eos")])
    fake = FakeLlama(probe=lambda r: UNSURE, think=lambda r: next(chunks))
    t = run_think(fake, s)
    assert t.finished and not t.clear
    assert len(t.tokens) == 50 and EOM not in t.tokens and EOT not in t.tokens
    final = fake.kinds("probe")[-1]
    assert final.text.endswith("x" * 10 + THINK_CLOSE + ANSWER)
    assert final.text.count(THINK_CLOSE) == 1 and len(fake.kinds("think")) == 2


def test_eot_also_ends_the_loop(s):
    fake = FakeLlama(probe=lambda r: sure("Reading"), think=lambda r: (words(30) + [EOT], "eos"))
    t = run_think(fake, s)
    assert t.finished and t.clear and len(t.tokens) == 30


def test_stop_text_put_back_when_server_drops_it(s):
    chunks = iter([(words(30), "word"), (words(30), "word")])
    fake = FakeLlama(probe=lambda r: sure("Reading") if r.think_n > 40 else UNSURE,
                     think=lambda r: next(chunks))
    t = run_think(fake, s)
    assert detok(t.tokens) == "x" * 30 + "\n\n" + "x" * 30 + "\n\n"


# ---- tiers and decisions -------------------------------------------------------------------------


def decider(fake: FakeLlama, s: Settings) -> Decider:
    return Decider(fake.client(s), s)


def by_question(table: dict) -> callable:
    """A probe script keyed by the question text's first words."""
    def probe(r: Req) -> dict:
        return next(v for k, v in table.items() if r.question.startswith(k))
    return probe


def test_assignment_tiers(s):
    fake = FakeLlama(
        probe=by_question({
            "What type": sure("Reading"),
            "Which prep": sure("Document"),
            "How much effort": {"One solid session": 0.5, "Two or three sessions": 0.45},
        }),
        think=lambda r: (words(30) + [EOM], "eos"),
    )
    out = decider(fake, s).decide_assignment(ASSIGNMENT)
    d = out["decisions"]
    assert list(d) == ["type", "template", "effort"]
    assert d["type"]["value"] == "reading" and d["type"]["tier"] == "instant"
    assert d["type"]["option_index"] == 2 and d["type"]["flipped"] is False
    assert d["template"]["tier"] == "instant" and d["template"]["value"] == "document"
    assert d["effort"]["tier"] == "guessed" and d["effort"]["flipped"] is None
    assert d["effort"]["think_tokens"] == 30
    assert [e["think_tokens"] for e in d["effort"]["trace"]] == [0, 30]
    assert d["type"]["model"] == "Muse-Glimmer-fake" and d["type"]["prompt_version"] == "t1"
    assert out["timings"]["probes"] == 6 and out["timings"]["think_calls"] == 1


def test_probe_tokens_after_first_are_only_the_tail(s):
    fake = FakeLlama(probe=lambda r: sure(r.options["A"]))  # flips, so every field thinks
    fake.think_fn = lambda r: (words(30, "\n\n") + [EOM], "eos")
    out = decider(fake, s).decide_assignment(ASSIGNMENT)
    probes = [(r, t) for k, r, t in fake.requests if k == "probe"]
    assert len(probes) == 9 and probes[0][1]["prompt_n"] == len(probes[0][0].ids)
    for r, t in probes[1:]:  # never the system turn or the assignment again
        tail = len(tok(r.text[r.text.rindex("Question: "):]))
        assert 0 < t["prompt_n"] <= tail
    assert out["decisions"]["type"]["probe_tokens"] == sum(t["prompt_n"] for _, t in probes[:2]) + \
        probes[6][1]["prompt_n"]  # type's forward and reversed probes, then its final probe


def test_lines_file_and_skip_thinking(s, tmp_path):
    (tmp_path / "lines.json").write_text(json.dumps({
        "lines": {"type": {"fast": 0.99, "think": 0.99}}, "skip_thinking": ["effort"]}))
    fake = FakeLlama(probe=by_question({"What type": {"Reading": 0.9, "Lab report": 0.09},
                                        "How much effort": {"Multi-day": 0.6, "One short sitting": 0.35}}),
                     think=lambda r: ([], "eos"))
    out = decider(fake, s).decide_assignment(ASSIGNMENT, ["type", "effort"])
    assert out["decisions"]["type"]["tier"] == "guessed"  # 0.91 < 0.99: unsure, thought, fell short
    assert out["decisions"]["effort"]["tier"] == "guessed"  # unsure and cheap: no thinking
    assert out["decisions"]["effort"]["value"] == "multi_day"
    assert out["decisions"]["effort"]["think_tokens"] == 0
    assert len(fake.kinds("think")) == 1  # only type thought


def test_template_question_has_hint_and_reply_line():
    block = choice_for(TEMPLATE).block()
    assert "Usual template by assignment type: Test or quiz: Dashboard;" in block
    assert block.splitlines()[-1] == "Reply with just the letter."
    assert "A) Document" in block and "E) Quick task" in block


def test_system_turn_identical_and_description_capped(s):
    fake = FakeLlama(probe=lambda r: sure(r.options["A"]))
    long = {**ASSIGNMENT, "description": "y" * 5000}
    decider(fake, s).decide_assignment(long, ["type"])
    decider(fake, s).decide_assignment(ASSIGNMENT, ["type"])
    systems = {r.text.split("<|eot|>")[0] for _, r, _ in fake.requests}
    assert systems == {"<|begin_of_text|><|start|>system<|message|>" + SYSTEM}
    assert "y" * DESCRIPTION_CHARS + "\n\nQuestion:" in fake.requests[0][1].text
    assert "y" * (DESCRIPTION_CHARS + 1) not in fake.requests[0][1].text


# ---- 6: topic naming -----------------------------------------------------------------------------

TOPICS = ["verbs_with_od_oi", "object_pronouns"]
QUESTIONS = [
    {"id": 1, "text": "Me lo dio.", "answer": "se lo", "marks": "X", "note": "", "points": 2},
    {"id": 2, "text": "Te la doy.", "answer": "te la", "marks": "✓", "note": "", "points": 2},
]


def test_topic_naming_reuses_an_existing_topic(s):
    fake = FakeLlama(probe=by_question({"What topic": sure("None of these fit"),
                                        "What format": sure("Fill in the blank"),
                                        "How much credit": sure("None")}),
                     name=lambda r: '"Object Pronouns."')
    out = decider(fake, s).decide_test(TOPICS, QUESTIONS[:1])
    d = out["decisions"]["1"]["topic"]
    assert d["value"] == "object_pronouns" and d["option_index"] == 1 and d["tier"] == "instant"
    assert out["new_topics"] == []
    name_req = fake.kinds("name")[0]
    assert name_req.text.endswith("write that one exactly.\nReply with just the topic name.<|eot|>"
                                  + THINK_OPEN + THINK_CLOSE + ANSWER)


def test_an_echo_of_the_question_is_never_a_topic(s):
    fake = FakeLlama(probe=by_question({"What topic": sure("None of these fit"),
                                        "What format": sure("Fill in the blank"),
                                        "How much credit": sure("None")}),
                     name=lambda r: "None of the listed topics fit.")
    out = decider(fake, s).decide_test(TOPICS, QUESTIONS[:1])
    d = out["decisions"]["1"]["topic"]
    assert d["value"] == "none_fit" and d["option_index"] == 2 and out["new_topics"] == []


def test_topic_naming_adds_a_new_topic_for_later_questions(s):
    fake = FakeLlama(
        probe=by_question({"What topic": sure("None of these fit"), "What format": sure("Fill in the blank"),
                           "How much credit": sure("None")}),
        name=lambda r: "Double object pronouns",
    )
    out = decider(fake, s).decide_test(TOPICS, QUESTIONS)
    assert out["new_topics"] == ["double_object_pronouns"]
    first, second = out["decisions"]["1"]["topic"], out["decisions"]["2"]["topic"]
    assert first["value"] == "double_object_pronouns" and first["option_index"] == 2
    assert second["value"] == "double_object_pronouns"  # named again, matched the new topic
    q2_forward, q2_reversed = [r for r in fake.kinds("probe") if r.question.startswith("What topic")][-2:]
    assert "- double object pronouns\n" in q2_forward.text  # in the class topic list up top
    assert "C) double object pronouns" in q2_forward.text and "D) None of these fit" in q2_forward.text
    assert "A) double object pronouns" in q2_reversed.text and "D) None of these fit" in q2_reversed.text
    assert len(fake.kinds("name")) == 2


def test_no_naming_when_none_fit_is_only_a_guess(s):
    fake = FakeLlama(
        probe=by_question({"What topic": {"None of these fit": 0.5, "object pronouns": 0.45},
                           "What format": sure("Fill in the blank"), "How much credit": sure("None")}),
        think=lambda r: (words(30) + [EOM], "eos"),
        name=lambda r: "should not be asked",
    )
    out = decider(fake, s).decide_test(TOPICS, QUESTIONS[:1])
    d = out["decisions"]["1"]["topic"]
    assert d["value"] == "none_fit" and d["tier"] == "guessed"
    assert fake.kinds("name") == [] and out["new_topics"] == []


def test_test_runs_fast_passes_first_then_loops(s):
    fake = FakeLlama(
        probe=by_question({"What topic": {"verbs with od oi": 0.5, "object pronouns": 0.45},
                           "What format": sure("Fill in the blank"), "How much credit": sure("Full")}),
        think=lambda r: (words(30) + [EOM], "eos"),
    )
    out = decider(fake, s).decide_test(TOPICS, QUESTIONS)
    fast = [i for i, (k, r, _) in enumerate(fake.requests) if k == "probe" and r.think_n == 0]
    thinks = [i for i, (k, _, _) in enumerate(fake.requests) if k == "think"]
    assert len(fast) == 2 * 5 and len(thinks) == 2  # format and credit twice each, topic once
    assert max(fast) < min(thinks)
    assert [d["topic"]["tier"] for d in out["decisions"].values()] == ["guessed", "guessed"]


def test_parallel_loops_each_get_their_own_slot(s):
    import threading
    import time

    gate = threading.Barrier(3, timeout=5)  # all three loops must be thinking at once

    def slow_think(r: Req):
        if r.think_n == 0:
            gate.wait()
        time.sleep(0.01)
        return words(30) + [EOM], "eos"

    fake = FakeLlama(probe=lambda r: sure(r.options["A"]), think=slow_think)  # flips: all think
    out = decider(fake, replace(s, parallel_slots=3)).decide_assignment(ASSIGNMENT)
    first_chunks = [r for r in fake.kinds("think") if r.think_n == 0]
    assert sorted(r.body["id_slot"] for r in first_chunks) == [0, 1, 2]
    assert all(d["tier"] == "thought" for d in out["decisions"].values())


# ---- the HTTP API ---------------------------------------------------------------------------------


@pytest.fixture
def api(s):
    fake = FakeLlama(probe=by_question({
        "What type": sure("Reading"), "Which prep": sure("Document"), "How much effort": sure("One solid session"),
        "What topic": sure("object pronouns"), "What format": sure("Fill in the blank"),
        "How much credit": sure("None"),
    }))
    main._decider = decider(fake, s)
    yield TestClient(main.app), fake
    main._decider = None


def test_api_health(api):
    client, _ = api
    assert client.get("/health").json() == {"ok": True, "llama_ok": True, "build": "b12023-fake"}


def test_api_decide_assignment(api):
    client, _ = api
    r = client.post("/decide/assignment", json={"assignment": {**ASSIGNMENT, "id": "7", "kind": "assignment"}})
    assert r.status_code == 200
    d = r.json()["decisions"]
    assert {k: v["value"] for k, v in d.items()} == {
        "type": "reading", "template": "document", "effort": "one_solid_session"}
    assert set(d["type"]) == {"value", "option_index", "confidence", "letter_share", "flipped", "tier",
                              "think_tokens", "probe_tokens", "trace", "prompt_version", "model"}
    assert client.post("/decide/assignment", json={"assignment": ASSIGNMENT, "fields": ["nope"]}).status_code == 422


def test_api_decide_test(api):
    client, _ = api
    r = client.post("/decide/test", json={"class_id": 2002, "topics": TOPICS, "questions": QUESTIONS,
                                          "fields": ["topic", "credit"]})
    assert r.status_code == 200
    body = r.json()
    assert body["decisions"]["2"]["topic"]["value"] == "object_pronouns"
    assert set(body["decisions"]["1"]) == {"topic", "credit"} and body["new_topics"] == []


def test_api_rejects_too_many_topics(api):
    client, _ = api
    topics = [f"t{i}" for i in range(26)]
    r = client.post("/decide/test", json={"class_id": 1, "topics": topics, "questions": QUESTIONS})
    assert r.status_code == 422


def test_api_llama_down_is_503(s):
    def down(request):
        raise httpx.ConnectError("refused")
    main._decider = Decider(Llama(s.glimmer_url, httpx.Client(transport=httpx.MockTransport(down))), s)
    try:
        client = TestClient(main.app)
        assert client.get("/health").json() == {"ok": False, "llama_ok": False, "build": ""}
        assert client.post("/decide/assignment", json={"assignment": ASSIGNMENT}).status_code == 503
    finally:
        main._decider = None
