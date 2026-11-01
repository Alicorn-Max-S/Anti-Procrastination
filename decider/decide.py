"""Deciding whole items: the fast pass for every field first, then the thinking loops for the fields
that came out unsure. Loops run in parallel, one llama-server slot each, only when PARALLEL_SLOTS > 1.
An assignment goes in two stages: type, then template and effort with the type answer in the prompt.

The tiers: `instant` when the forward and reversed fast-pass probes agree and clear the fast line;
`thought` when a thinking probe clears the think line; `guessed` for the top option otherwise (a
cheap field in skip_thinking, Glimmer ending its thinking below the line, or the cap).
"""

import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from queue import Queue
from typing import Callable

from decisions.fields import ASSIGNMENT_FIELDS, QUESTION_FIELDS, Field

from .config import Lines, Settings, load_lines
from .gate import FastPass, fast_pass
from .llama import Llama
from .probe import Probe
from .prompts import (
    MAX_TOPICS,
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
from .think import Thought, think
from .topics import name_topic, resolve


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


class Decider:
    def __init__(self, llama: Llama, settings: Settings) -> None:
        self.llama = llama
        self.s = settings
        self._lock = threading.Lock()  # one item at a time: interleaved items would thrash the cache
        self._topics_lock = threading.Lock()

    # ---- one field ----------------------------------------------------------------------------

    def _decision(self, job: Job, tier: str) -> dict:
        fp = job.fast
        entries = [(0, fp.forward, job.fast_choice)]
        if job.thought:
            entries += [(n, p, job.choice) for n, p in job.thought.trace]
        _, last, choice = entries[-1]
        probes = [fp.forward, fp.reversed] + ([p for _, p in job.thought.trace] if job.thought else [])
        return {
            "value": choice.options[last.top].value,
            "option_index": last.top,
            "confidence": round(last.confidence, 6),
            "letter_share": round(last.letter_share, 6),
            "flipped": fp.flipped,
            "tier": tier,
            "think_tokens": len(job.thought.tokens) if job.thought else 0,
            "probe_tokens": sum(p.probe_tokens for p in probes if p is not None),
            "trace": [_entry(n, p, c) for n, p, c in entries],
            "prompt_version": self.s.prompt_version,
            "model": self.llama.model(),
        }

    def _settle(self, job: Job, lines: Lines, slot: int = 0) -> dict | None:
        """The fast pass. Returns the decision when no thinking is needed, else None."""
        job.fast_choice = job.choice
        job.fast = fast_pass(self.llama, job.context, job.choice, lines.fast(job.field.key),
                             self.s.letter_share_min, self.s.fast_pass_format, slot)
        job.prompt = job.fast.prompt
        if job.fast.instant:
            return self._decision(job, "instant")
        if job.field.key in lines.skip_thinking:
            return self._decision(job, "guessed")
        return None

    def _think(self, job: Job, lines: Lines, slot: int, seed: int | None) -> dict:
        job.thought = think(self.llama, job.prompt, job.choice, lines.think(job.field.key), self.s,
                            slot, seed)
        return self._decision(job, "thought" if job.thought.clear else "guessed")

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
                          seed: int | None = None) -> dict:
        """Type first, then template and effort with the type answer in their prompt (USES_TYPE).
        Type is decided even when only a later field is asked for."""
        wanted = pick_fields(ASSIGNMENT_FIELDS, fields)
        later = [f for f in wanted if f.key in USES_TYPE]
        first = [f for f in ASSIGNMENT_FIELDS
                 if f.key not in USES_TYPE and (f in wanted or (bool(later) and f.key == "type"))]
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
                stage(later, with_type(context, decisions["type"]["value"]))
            return {
                "decisions": {f.key: decisions[f.key] for f in wanted},
                "timings": self._timings(start, before),
            }

    def decide_test(self, topics: list[str], questions: list[dict], fields: list[str] | None = None,
                    course: str = "", seed: int | None = None) -> dict:
        """Questions in order: the fast pass for every field of every question, then the thinking
        loops. A topic named along the way joins the class list for the questions after it."""
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
                """Name the topic when "None of these fit" was decided confidently."""
                none = none_fit_index(job.choice)
                if none is None or d["option_index"] != none or d["tier"] == "guessed":
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
                    d = self._settle(job, lines)
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
