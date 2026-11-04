"""Deciding whole items: the fast pass for every field first, then the thinking loops for the fields
that came out unsure. Loops run in parallel, one llama-server slot each, only when PARALLEL_SLOTS > 1.
An assignment goes in two stages: type, then template and effort with the type answer in the prompt.

The tiers: `instant` when the forward and reversed fast-pass probes agree and clear the fast line;
`thought` when a thinking probe clears the think line, or Glimmer closes its thinking on the fast
pass's own answer; `guessed` when thinking ends on another answer or runs to the cap, and for a field
in skip_thinking (format, topic and credit, so test questions go on the fast pass alone); `default`
for a template the type settles (prompts.TEMPLATE_FROM_TYPE).

`clarify` marks what goes on the student's clarify list: a guessed type or template. A guessed
effort only gets the badge, and topic and credit never go on it.

`calibrated_confidence` is the decision's chance of being right: its last probe at the field's
fitted temperature for that kind of probe, fast pass or thinking (decisions/lines.json).
`confidence` stays the raw top share the lines are on.

In joint mode (settings.mode, decider/joint.py) an assignment's fields share one prompt and one
thinking chain instead of two stages; test questions are decided per field either way.
"""

import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from queue import Queue
from typing import Callable

from decisions.fields import ASSIGNMENT_FIELDS, QUESTION_FIELDS, Field, field_values

from .config import MODES, Lines, Settings, load_lines
from .gate import FastPass, fast_pass
from .joint import JointPrompt, probe_round
from .llama import Llama
from .probe import Probe, calibrated, clears
from .prompts import (
    MAX_TOPICS,
    TEMPLATE_FROM_TYPE,
    USES_TYPE,
    Choice,
    Prompt,
    TopicListFull,
    assignment_context,
    choice_for,
    none_fit_index,
    test_context,
    with_type,
)
from .think import Thought, chain, think
from .topics import name_topic, resolve


CLARIFY_FIELDS = ("type", "template")  # a guessed answer here goes on the clarify list
FIELD_BY_KEY = {f.key: f for f in ASSIGNMENT_FIELDS}


class UnknownField(ValueError):
    pass


def pick_fields(all_fields: tuple[Field, ...], names: list[str] | None) -> list[Field]:
    if names is None:
        return list(all_fields)
    known = {f.key for f in all_fields}
    bad = [n for n in names if n not in known]
    if bad:
        raise UnknownField(f"unknown fields {bad}; choose from {sorted(known)}")
    return [f for f in all_fields if f.key in names]


@dataclass
class Job:
    """One field of one item on its way through the fast pass and maybe the thinking loop."""

    item: str  # the question id for a test, "" for an assignment
    field: Field
    context: str
    choice: Choice
    fast: FastPass | None = None
    fast_choice: Choice | None = None  # the choice the fast pass saw (topics can grow after it)
    thought: Thought | None = None
    prompt: Prompt | None = None  # what the thinking loop runs on


def _entry(think_tokens: int, p: Probe, choice: Choice) -> dict:
    return {
        "think_tokens": think_tokens,
        "probs": {o.value: round(p.probs[i], 6) for i, o in enumerate(choice.options)},
        "letter_share": round(p.letter_share, 6),
    }


def values(choice: Choice) -> list[str]:
    return [o.value for o in choice.options]


class Decider:
    def __init__(self, llama: Llama, settings: Settings) -> None:
        if settings.mode not in MODES:
            raise ValueError(f"DECIDER_MODE {settings.mode!r}: choose from {MODES}")
        self.llama = llama
        self.s = settings
        self._lock = threading.Lock()  # one item at a time: interleaved items would thrash the cache
        self._topics_lock = threading.Lock()

    # ---- one field ----------------------------------------------------------------------------

    def _made(self, f: Field, entries: list[tuple[int, Probe, Choice]], probes: list[Probe | None],
              flipped: bool | None, tier: str, think_tokens: int, lines: Lines) -> dict:
        """A Decision: its answer is the last entry's top option (the fast pass is entry 0)."""
        _, last, choice = entries[-1]
        return {
            "value": choice.options[last.top].value,
            "option_index": last.top,
            "confidence": round(last.confidence, 6),
            "calibrated_confidence": round(calibrated(last.probs, lines.temperature(f.key, len(entries) > 1)), 6),
            "letter_share": round(last.letter_share, 6),
            "flipped": flipped,
            "tier": tier,
            "think_tokens": think_tokens,
            "probe_tokens": sum(p.probe_tokens for p in probes if p is not None),
            "trace": [_entry(n, p, c) for n, p, c in entries],
            "prompt_version": self.s.prompt_version,
            "model": self.llama.model(),
            "clarify": tier == "guessed" and f.key in CLARIFY_FIELDS,
        }

    def _decision(self, job: Job, tier: str, lines: Lines) -> dict:
        fp = job.fast
        entries = [(0, fp.forward, job.fast_choice)]
        if job.thought:
            entries += [(n, p, job.choice) for n, p in job.thought.trace]
        probes = [fp.forward, fp.reversed] + ([p for _, p in job.thought.trace] if job.thought else [])
        return self._made(job.field, entries, probes, fp.flipped, tier,
                          len(job.thought.tokens) if job.thought else 0, lines)

    def _from_type(self, f: Field, value: str) -> dict:
        """A template the type settles: no question asked, so no probes and no trace."""
        return {
            "value": value, "option_index": field_values(f).index(value), "confidence": 1.0,
            "calibrated_confidence": 1.0, "letter_share": 1.0, "flipped": None, "tier": "default",
            "think_tokens": 0, "probe_tokens": 0, "trace": [], "prompt_version": self.s.prompt_version,
            "model": self.llama.model(), "clarify": False,
        }

    def _settle(self, job: Job, lines: Lines, slot: int = 0, think: bool = True) -> dict | None:
        """The fast pass. Returns the decision when no thinking is needed, else None. With `think`
        off, nothing thinks: an unsure field takes the fast pass's top option (tier `guessed`)."""
        job.fast_choice = job.choice
        job.fast = fast_pass(self.llama, job.context, job.choice, lines.fast(job.field.key),
                             self.s.letter_share_min, self.s.fast_pass_format, slot,
                             lines.offsets(job.field.key, values(job.choice)))
        job.prompt = job.fast.prompt
        if job.fast.instant:
            return self._decision(job, "instant", lines)
        if job.field.key in lines.skip_thinking or not think:
            return self._decision(job, "guessed", lines)
        return None

    def _think(self, job: Job, lines: Lines, slot: int, seed: int | None) -> dict:
        job.thought = think(self.llama, job.prompt, job.choice, lines.think(job.field.key), self.s,
                            slot, seed)
        t = job.thought
        fast = job.fast_choice.options[job.fast.forward.top].value
        agrees = t.finished and job.choice.options[t.last.top].value == fast
        return self._decision(job, "thought" if t.clear or agrees else "guessed", lines)

    def _loops(self, jobs: list[Job], run: Callable[[Job, int], None]) -> None:
        if self.s.parallel_slots <= 1 or len(jobs) <= 1:
            for job in jobs:
                run(job, 0)
            return
        slots: Queue = Queue()
        for i in range(self.s.parallel_slots):
            slots.put(i)

        def pinned(job: Job) -> None:
            slot = slots.get()
            try:
                run(job, slot)
            finally:
                slots.put(slot)

        with ThreadPoolExecutor(self.s.parallel_slots) as pool:
            list(pool.map(pinned, jobs))  # list() re-raises the first error

    def _timings(self, start: float, before: Counter) -> dict:
        d = Counter(self.llama.counts)
        d.subtract(before)
        return {
            "ms": round((time.perf_counter() - start) * 1000),
            "probes": d["probe"],
            "think_calls": d["think"],
            "name_calls": d["name"],
            "prompt_tokens": d["prompt_n"],
            "generated_tokens": d["predicted_n"],
        }

    # ---- items --------------------------------------------------------------------------------

    def decide_assignment(self, assignment: dict, fields: list[str] | None = None,
                          seed: int | None = None, type_value: str | None = None) -> dict:
        """Type first, then template and effort with the type answer in their prompt (USES_TYPE).
        Type is decided even when only a later field is asked for. `type_value` is a type the
        student confirmed: it isn't decided again (nor returned), and the later fields use it."""
        wanted = pick_fields(ASSIGNMENT_FIELDS, fields)
        if type_value is not None and type_value not in field_values(FIELD_BY_KEY["type"]):
            raise UnknownField(f"unknown type {type_value!r}; choose from {field_values(FIELD_BY_KEY['type'])}")
        if self.s.mode == "joint":
            return self._decide_joint(assignment, wanted, seed, type_value)
        later = [f for f in wanted if f.key in USES_TYPE]
        need_type = type_value is None and (FIELD_BY_KEY["type"] in wanted or bool(later))
        first = [FIELD_BY_KEY["type"]] if need_type else []
        with self._lock:
            start, before = time.perf_counter(), Counter(self.llama.counts)
            lines = load_lines(self.s.lines_file)
            context = assignment_context(assignment)
            decisions: dict[str, dict] = {}

            def run(job: Job, slot: int) -> None:
                decisions[job.field.key] = self._think(job, lines, slot, seed)

            def stage(group: list[Field], ctx: str) -> None:
                pending = []
                for f in group:
                    job = Job("", f, ctx, choice_for(f))
                    d = self._settle(job, lines)
                    if d is None:
                        pending.append(job)
                    else:
                        decisions[f.key] = d
                self._loops(pending, run)

            stage(first, context)
            if later:
                t = type_value or decisions["type"]["value"]
                if self.s.template_from_type and t in TEMPLATE_FROM_TYPE and FIELD_BY_KEY["template"] in later:
                    decisions["template"] = self._from_type(FIELD_BY_KEY["template"], TEMPLATE_FROM_TYPE[t])
                    later = [f for f in later if f.key != "template"]
                stage(later, with_type(context, t))
            return {
                "decisions": {f.key: decisions[f.key] for f in wanted if f.key in decisions},
                "timings": self._timings(start, before),
            }

    def _decide_joint(self, assignment: dict, wanted: list[Field], seed: int | None,
                      type_value: str | None) -> dict:
        """Joint mode: a forward round of probes (every field, each seeing the earlier fields' top
        answers), a reversed round for the fields that clear their fast line, then one thinking
        chain for every field still unsure, probing those after each paragraph until each clears
        its think line. Tiers and clarify mean what they do per field. Every field is decided;
        `wanted` only picks what's returned."""
        with self._lock:
            start, before = time.perf_counter(), Counter(self.llama.counts)
            lines = load_lines(self.s.joint_lines_file)
            share = self.s.letter_share_min
            choices = [choice_for(f) for f in ASSIGNMENT_FIELDS]
            by_key = {c.field: c for c in choices}
            prompt = JointPrompt(self.llama, assignment_context(assignment), choices, self.s.fast_pass_format)
            offsets = {c.field: lines.offsets(c.field, values(c)) for c in choices}
            written: dict[str, str] = {} if type_value is None else {"type": type_value}
            asked = [c.field for c in choices if c.field not in written]

            def top(f: str, p: Probe) -> str:
                return by_key[f].options[p.top].value

            def settles(type_now: str | None) -> bool:
                return self.s.template_from_type and type_now in TEMPLATE_FROM_TYPE

            forward = probe_round(self.llama, prompt, [], written, set(asked), offsets=offsets)
            fast = {f: top(f, p) for f, p in forward.items()}
            passed = {f for f, p in forward.items() if clears(p, lines.fast(f), share)}
            back = {}
            if passed:
                unsure = {f: v for f, v in fast.items() if f not in passed}
                back = probe_round(self.llama, prompt.reversed(), [], written | unsure, passed, offsets=offsets)
            tiers: dict[str, str] = {}
            for f in asked:
                b = back.get(f)
                if b is not None and b.top == forward[f].top and b.letter_share >= share:
                    tiers[f] = "instant"
                elif f in lines.skip_thinking:
                    tiers[f] = "guessed"
                if f in tiers:
                    written[f] = fast[f]
            if "template" in asked and settles(written.get("type")):
                tiers["template"] = "default"
                written["template"] = TEMPLATE_FROM_TYPE[written["type"]]

            need = [f for f in asked if f not in tiers]
            traces: dict[str, list[tuple[int, Probe]]] = {f: [] for f in need}
            cleared: dict[str, int] = {}  # field -> thinking tokens when its probe cleared the line
            probing = set(need)

            def after(tokens: list[int]) -> bool:
                round_ = probe_round(self.llama, prompt, tokens, written, probing)
                for f, p in round_.items():
                    traces[f].append((len(tokens), p))
                    if clears(p, lines.think(f), share):
                        cleared[f] = len(tokens)
                        written[f] = top(f, p)
                        probing.discard(f)
                type_now = written.get("type") or (top("type", round_["type"]) if "type" in round_ else None)
                # a template the type settles needn't clear (it's still probed, in case the type moves)
                return not [f for f in probing if not (f == "template" and settles(type_now))]

            tokens, finished = chain(self.llama, prompt, self.s, 0, seed, after) if need else ([], False)
            out: dict[str, dict] = {}
            for f in asked:
                c = by_key[f]
                if f in need:
                    if f in cleared:
                        tiers[f] = "thought"
                    else:
                        agrees = finished and top(f, traces[f][-1][1]) == fast[f]
                        tiers[f] = "thought" if agrees else "guessed"
                if tiers[f] == "default":
                    continue
                entries = [(0, forward[f], c)] + [(n, p, c) for n, p in traces.get(f, [])]
                probes = [forward[f], back.get(f)] + [p for _, p in traces.get(f, [])]
                flipped = None if f not in back else back[f].top != forward[f].top
                think_tokens = cleared.get(f, len(tokens)) if f in need else 0
                out[f] = self._made(FIELD_BY_KEY[f], entries, probes, flipped, tiers[f], think_tokens, lines)
            final_type = type_value or out["type"]["value"]
            if "template" in asked and settles(final_type):
                out["template"] = self._from_type(FIELD_BY_KEY["template"], TEMPLATE_FROM_TYPE[final_type])
            return {
                "decisions": {f.key: out[f.key] for f in wanted if f.key in out},
                "timings": self._timings(start, before),
            }

    def decide_test(self, topics: list[str], questions: list[dict], fields: list[str] | None = None,
                    course: str = "", seed: int | None = None, think: bool = True) -> dict:
        """Questions in order: the fast pass for every field of every question, then the thinking
        loops. A topic named along the way joins the class list for the questions after it.
        `think` off is the fast pass alone, whatever the lines file says (the app labels graded
        tests this way)."""
        wanted = pick_fields(QUESTION_FIELDS, fields)
        topics = list(topics)
        if len(topics) > MAX_TOPICS:
            raise TopicListFull(f"{len(topics)} topics; at most {MAX_TOPICS} fit one letter each")
        with self._lock:
            start, before = time.perf_counter(), Counter(self.llama.counts)
            lines = load_lines(self.s.lines_file)
            new_topics: list[str] = []
            out: dict[str, dict[str, dict]] = {str(q["id"]): {} for q in questions}
            by_id = {str(q["id"]): q for q in questions}

            def build(qid: str, f: Field) -> Job:
                q = by_id[qid]
                return Job(qid, f, test_context(q, topics, course), choice_for(f, topics))

            def settle_topic(job: Job, d: dict, slot: int) -> dict:
                """Name the topic when "None of these fit" was decided confidently: instantly, or by
                a thinking probe that cleared the line (agreeing with the fast pass isn't enough)."""
                none = none_fit_index(job.choice)
                sure = d["tier"] == "instant" or (job.thought is not None and job.thought.clear)
                if none is None or d["option_index"] != none or not sure:
                    return d
                name = name_topic(self.llama, job.context, self.s.fast_pass_format, slot)
                if not name:
                    return d
                with self._topics_lock:
                    topic, new = resolve(name, topics)
                    if new:
                        if len(topics) >= MAX_TOPICS:
                            raise TopicListFull(f"naming {topic!r} would make {len(topics) + 1} topics")
                        topics.append(topic)
                        new_topics.append(topic)
                    return {**d, "value": topic, "option_index": topics.index(topic)}

            pending: list[Job] = []
            for qid in out:
                for f in wanted:
                    job = build(qid, f)
                    d = self._settle(job, lines, think=think)
                    if d is None:
                        pending.append(job)
                    else:
                        out[qid][f.key] = settle_topic(job, d, 0)

            def run(job: Job, slot: int) -> None:
                if job.field.per_class:
                    with self._topics_lock:
                        fresh = build(job.item, job.field)
                    if fresh.choice != job.choice:  # topics were named since its fast pass
                        job.context, job.choice = fresh.context, fresh.choice
                        job.prompt = Prompt(self.llama, job.context, job.choice.block(),
                                            self.s.fast_pass_format)
                d = self._think(job, lines, slot, seed)
                out[job.item][job.field.key] = settle_topic(job, d, slot) if job.field.per_class else d

            self._loops(pending, run)
            return {
                "decisions": {qid: {f.key: fs[f.key] for f in wanted} for qid, fs in out.items()},
                "new_topics": new_topics,
                "timings": self._timings(start, before),
            }
