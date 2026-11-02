"""calibrate (spec, Part C): System One over every labeled item with no gate, then proposed lines.

For every item and field it records the forward fast-pass probe, the reversed probe and a full
thinking loop up to the cap, with the probe after every paragraph. The loop's think line is out of
reach, so it only ends when Glimmer closes its thinking or the cap is hit. The decider's loop runs
the same prompts with the same seed and stops at the first probe that clears its line, so any pair
of lines can be replayed from these records without another model call.

Proposed lines:
- expensive fields: the lowest line that accepts zero labeled errors;
- cheap fields: the lowest line that accepts no more than 10% errors;
- skip_thinking: every cheap field whose fast-pass error is 10% or less, plus SKIP_THINKING (any cost).
The fast line is scored on what the gate acts on (the forward probe clears it and the reversed probe
agrees); the think line is replayed on the fields the proposed fast line leaves unsure.

Two fits come first and last (written to the lines file too):
- option offsets: a log offset per option on the fast pass, fitted to the labels, kept only when
  they lift the field's fast pass by OFFSET_MIN_GAIN under 5-fold cross-validation (credit's fast
  pass leaned towards "none"). The lines are then fitted on the shifted probes.
- temperatures: two per field, for decisions from the fast pass and from a thinking probe, fitted to
  the decisions the proposed lines make, so a decision's calibrated_confidence is its chance of
  being right. One temperature for both can't fit: fast-pass probes are spread out, thinking probes
  put everything on one letter. The report shows both checks held out.

Joint mode (--mode joint, decider/joint.py) records each assignment's fields together, from one
prompt and one shared chain, into calibrate-joint.jsonl, one record per field in the same shape,
so the replay and the report are the same; its proposal goes to decisions/lines.joint.json.
"""

import json
import math
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from dataclasses import dataclass, replace
from statistics import mean, median

from decider.config import Settings
from decider.joint import JointPrompt, probe_round
from decider.llama import Llama
from decider.probe import Probe, calibrated, probe, shift, tempered
from decider.prompts import TEMPLATE_FROM_TYPE, USES_TYPE, Prompt, choice_for, with_type
from decider.think import chain, think
from decisions.fields import ASSIGNMENT_FIELDS, ASSIGNMENT_KIND, QUESTION_FIELDS, Field

from .items import EVAL_DATA, Item, Skipped

RECORDS = EVAL_DATA / "calibrate.jsonl"
JOINT_RECORDS = EVAL_DATA / "calibrate-joint.jsonl"
COST = {"type": "expensive", "template": "expensive", "effort": "cheap",
        "format": "cheap", "topic": "expensive", "credit": "expensive"}  # spec, Fields
CHEAP_MAX_ERROR = 0.10
# Think lines set by hand instead of by the zero-error rule. Credit stops thinking once a probe is
# this sure: on the v8 records it keeps the same 24 of 26 right and cuts about a third of the
# thinking. The same stop costs type and template 4 or 5 right answers each, so they stay at never.
THINK_STOPS = {"credit": 0.999}
# Fast lines set by hand instead of by the rule. Effort thinks only when its fast pass is under 0.80
# or the reversed probe disagrees (Nov 1 evening, replacing the skip): on the v9 records that gets
# back the 5 right answers the skip lost, for about the skip's speed (Claude's replay).
FAST_LINES = {"effort": 0.80}
# Fields that skip thinking by hand, whatever their cost or fast-pass error. Test questions go on the
# fast pass alone (Max, Nov 1, from the v10 compare): it matched full System One on format and credit
# and lost one topic answer of 51, at 0.8 s a question instead of 2.7.
SKIP_THINKING: set[str] = {"topic", "credit"}
OFFSET_FIELDS = ("type", "template", "effort", "format", "credit")  # topic's options are per class
OFFSET_MIN_GAIN = 0.03  # of the field's labeled cells, under cross-validation
FOLDS = 5
CLARIFY_FIELDS = ("type", "template")  # decider/decide.py: a guessed answer goes to the student
ORDINAL = ("effort",)  # options in order of size, so a near miss is scored too (within one level)
NEVER = 1.01  # a line no confidence reaches
OUT_OF_REACH = 2.0  # the think line while recording, so the loop runs to the cap
BINS = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 0.999, 1.0)
BAR = 24


def run_id(s: Settings, llama: Llama, seed: int) -> dict:
    """What a record depends on besides the item: a record made under another one is redone."""
    return {"prompt_version": s.prompt_version, "model": llama.model(), "seed": seed,
            "cap": s.cap_tokens, "paragraph": s.paragraph_max, "min_chunk": s.min_probe_chunk,
            "fast_pass_format": s.fast_pass_format}


def _probe(p: Probe) -> dict:
    return {"probs": [round(x, 6) for x in p.probs], "share": round(p.letter_share, 6),
            "conf": round(p.confidence, 6), "top": p.top, "probe_tokens": p.probe_tokens}


def record(llama: Llama, s: Settings, item: Item, f: Field, seed: int, slot: int = 0,
           type_answer: str | None = None) -> dict:
    """Forward probe, the full thinking loop on the forward prompt, then the reversed probe.
    Template and effort see System One's type answer, as in the decider (USES_TYPE)."""
    choice = choice_for(f, item.topics)
    context = item.context() if type_answer is None else with_type(item.context(), type_answer)
    t0 = time.perf_counter()
    prompt = Prompt(llama, context, choice.block(), s.fast_pass_format)
    forward = probe(llama, prompt.answer([]), choice, slot)
    t1 = time.perf_counter()
    thought = think(llama, prompt, choice, OUT_OF_REACH, s, slot, seed)
    t2 = time.perf_counter()
    flipped = choice.reversed()
    back = probe(llama, Prompt(llama, context, flipped.block(), s.fast_pass_format).answer([]), flipped, slot)
    t3 = time.perf_counter()
    return {
        "key": item.key, "kind": item.kind, "field": f.key, "label": item.labels.get(f.key),
        "options": [o.value for o in choice.options], "type_answer": type_answer,
        "forward": _probe(forward), "reversed": _probe(back),
        "trace": [{"think_tokens": n, **_probe(p)} for n, p in thought.trace],
        "finished": thought.finished, "think_tokens": len(thought.tokens),
        "seconds": {"forward": round(t1 - t0, 3), "think": round(t2 - t1, 3), "reversed": round(t3 - t2, 3)},
        "run": run_id(s, llama, seed),
    }


def record_joint(llama: Llama, s: Settings, item: Item, seed: int, slot: int = 0) -> list[dict]:
    """Joint mode: the forward round, the shared chain up to the cap with a round of probes for
    every field after each paragraph, then the reversed round. One record per field, in record()'s
    shape. Each probe sees the earlier fields' top answers in its own round; the decider writes a
    field's settled answer instead once it has one, so a replay can differ from it there."""
    choices = [choice_for(f, item.topics) for f in item.fields]
    every = {c.field for c in choices}
    t0 = time.perf_counter()
    prompt = JointPrompt(llama, item.context(), choices, s.fast_pass_format)
    forward = probe_round(llama, prompt, [], {}, every, slot)
    t1 = time.perf_counter()
    rounds: list[tuple[int, dict[str, Probe]]] = []

    def after(tokens: list[int]) -> bool:
        rounds.append((len(tokens), probe_round(llama, prompt, tokens, {}, every, slot)))
        return False  # no line: on to the cap, or until Glimmer closes its thinking

    tokens, finished = chain(llama, prompt, s, slot, seed, after)
    t2 = time.perf_counter()
    back = probe_round(llama, prompt.reversed(), [], {}, every, slot)
    t3 = time.perf_counter()
    run = run_id(s, llama, seed) | {"mode": "joint"}
    seconds = {"forward": round(t1 - t0, 3), "think": round(t2 - t1, 3), "reversed": round(t3 - t2, 3)}
    return [{
        "key": item.key, "kind": item.kind, "field": c.field, "label": item.labels.get(c.field),
        "options": [o.value for o in c.options], "type_answer": None,
        "forward": _probe(forward[c.field]), "reversed": _probe(back[c.field]),
        "trace": [{"think_tokens": n, **_probe(ps[c.field])} for n, ps in rounds],
        "finished": finished, "think_tokens": len(tokens), "seconds": seconds, "run": run,
    } for c in choices]


def records_path(mode: str = "fields") -> Path:
    return JOINT_RECORDS if mode == "joint" else RECORDS


def load_records(mode: str = "fields") -> dict[tuple[str, str], dict]:
    path = records_path(mode)
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        out[(r["key"], r["field"])] = r  # the last record for an (item, field) wins
    return out


def current(rec: dict | None, item: Item, f: Field, run: dict, type_answer: str | None = None) -> bool:
    return (rec is not None and rec["run"] == run and rec.get("type_answer") == type_answer
            and rec["options"] == [o.value for o in choice_for(f, item.topics).options])


def type_answers(items: list[Item], recs: dict[tuple[str, str], dict], run: dict | None,
                 share_min: float) -> dict[str, str]:
    """System One's own type answer per assignment, under the type lines these records propose:
    what the decider would put in the template and effort prompts once those lines are approved.
    `run` None takes each item's latest type record, whatever run made it (the report)."""
    have = {i.key: r for i in items if (r := recs.get((i.key, "type"))) and (run is None or r["run"] == run)}
    labeled = [{**r, "label": i.labels["type"]} for i in items
               if (r := have.get(i.key)) and "type" in i.labels]
    if not labeled:  # a smoke run with no labeled type: take where thinking ended
        return {k: r["options"][r["trace"][-1]["top"]] for k, r in have.items()}
    p = plan("type", labeled, share_min)
    return {k: r["options"][decide(with_offsets(r, p.offsets), p.fast, p.think, p.skip, share_min).top]
            for k, r in have.items()}


def record_all(llama: Llama, s: Settings, items: list[Item], seed: int, fresh: bool = False,
               slots: int = 1, mode: str = "fields") -> None:
    """Records every labeled field that has no record under this run yet, appending as it goes.

    With slots > 1 (llama-server started with -np N), N items are recorded at once, each on its own
    slot, so one item's fields still share their cached prefix. Batched decoding is several times
    faster in total. Its logits can differ from a single slot's in the last bits, so now and then a
    sampled thinking token differs from what the decider's one slot would sample.

    Two passes, as the decider goes: type for every assignment first (labeled or not, since template
    and effort need System One's answer), then every other labeled field. Joint mode records each
    assignment's fields together in one pass (test questions aren't decided jointly)."""
    if mode == "joint":
        run = run_id(s, llama, seed) | {"mode": "joint"}
        have = {} if fresh else load_records("joint")
        todo = [(i, list(i.fields), None) for i in items if i.kind == ASSIGNMENT_KIND
                and not all(current(have.get((i.key, f.key)), i, f, run) for f in i.fields)]
        print(f"calibrate (joint): seed {seed}, cap {s.cap_tokens}, {slots} slot{'s' if slots > 1 else ''}, "
              f"{len(todo)} assignments to record", flush=True)
        _record_jobs(llama, s, todo, seed, slots, "joint")
        return
    run = run_id(s, llama, seed)
    have = {} if fresh else load_records()
    type_field = next(f for f in ASSIGNMENT_FIELDS if f.key == "type")
    first = [(i, [type_field]) for i in items if i.kind == ASSIGNMENT_KIND
             and not current(have.get((i.key, "type")), i, type_field, run)]
    print(f"calibrate: seed {seed}, cap {s.cap_tokens}, {slots} slot{'s' if slots > 1 else ''}", flush=True)
    print(f"pass 1: type for {len(first)} assignments", flush=True)
    _record_jobs(llama, s, [(i, fs, None) for i, fs in first], seed, slots)

    answers = type_answers(items, load_records(), run, s.letter_share_min)
    have = {} if fresh else load_records()
    todo = []
    settled = 0
    for i in items:
        fs = []
        for f in i.fields:
            ta = answers.get(i.key) if f.key in USES_TYPE else None
            if f.key == "template" and from_type(s.template_from_type, ta):
                settled += "template" in i.labels
                continue  # the decider asks no template question for this type
            if f.key in i.labels and f.key != "type" and not current(have.get((i.key, f.key)), i, f, run, ta):
                fs.append(f)
        if fs:
            todo.append((i, fs, answers.get(i.key)))
    total = sum(len(fs) for _, fs, _ in todo)
    rest = sum(len([k for k in i.labels if k != "type"]) for i in items)
    print(f"pass 2: {total} fields to record ({rest - total - settled} already recorded, "
          f"{settled} templates set by the type)", flush=True)
    _record_jobs(llama, s, todo, seed, slots)


def from_type(on: bool, type_answer: str | None) -> str | None:
    """The template the type settles (prompts.TEMPLATE_FROM_TYPE), or None when it's asked."""
    return TEMPLATE_FROM_TYPE.get(type_answer) if on and type_answer else None


def _record_jobs(llama: Llama, s: Settings, todo: list[tuple[Item, list[Field], str | None]], seed: int,
                 slots: int, mode: str = "fields") -> None:
    """Records each item's fields on one slot, N items at once, appending as it goes."""
    total = sum(len(fs) for _, fs, _ in todo)
    if not total:
        return
    path = records_path(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    lock = threading.Lock()
    free: Queue = Queue()
    for slot in range(slots):
        free.put(slot)
    done = 0

    with path.open("a", encoding="utf-8") as out:
        def one_item(job: tuple[Item, list[Field], str | None]) -> None:
            nonlocal done
            item, fields, type_answer = job
            slot = free.get()
            try:
                if mode == "joint":
                    recs = record_joint(llama, s, item, seed, slot)
                else:  # one field at a time, each written as soon as it's recorded
                    recs = (record(llama, s, item, f, seed, slot, type_answer if f.key in USES_TYPE else None)
                            for f in fields)
                for r in recs:
                    fwd, last = r["forward"], r["trace"][-1]
                    right = r["options"].index(r["label"]) if r["label"] in r["options"] else None

                    def mark(top: int) -> str:
                        return "unlabeled" if right is None else "ok" if top == right else "WRONG"

                    with lock:
                        out.write(json.dumps(r) + "\n")
                        out.flush()
                        done += 1
                        eta = (time.perf_counter() - start) / done * (total - done) / 60
                        print(f"  [{done}/{total}] {item.key} {r['field']}: fast {fwd['conf']:.3f} "
                              f"{mark(fwd['top'])}, thinking {r['think_tokens']} tokens "
                              f"({'closed' if r['finished'] else 'cap'}) {mark(last['top'])}, "
                              f"{sum(r['seconds'].values()):.1f} s (eta {eta:.0f} min)", flush=True)
            finally:
                free.put(slot)

        with ThreadPoolExecutor(slots) as pool:
            list(pool.map(one_item, todo))  # list() re-raises the first error


# ---- replaying lines --------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    tier: str  # instant, thought or guessed, as in decider/decide.py
    top: int
    looped: bool  # the thinking loop ran
    cap_hit: bool
    think_tokens: int


def passes_gate(r: dict, line: float, share_min: float, reversed_check: bool = True) -> bool:
    f = r["forward"]
    if f["conf"] < line or f["share"] < share_min:
        return False
    b = r["reversed"]
    return not reversed_check or (b["top"] == f["top"] and b["share"] >= share_min)


def loop_end(r: dict, line: float, share_min: float) -> Outcome:
    """Where the decider's loop stops: the first probe that clears, else the end of the trace
    (Glimmer closed its thinking below the line, or the cap)."""
    for e in r["trace"]:
        if e["conf"] >= line and e["share"] >= share_min:
            return Outcome("thought", e["top"], True, False, e["think_tokens"])
    last = r["trace"][-1]
    return Outcome("guessed", last["top"], True, not r["finished"], last["think_tokens"])


def decide(r: dict, fast: float, think_line: float, skip: bool, share_min: float) -> Outcome:
    """The decider's tiers: a loop that ends below the line still counts as thought when Glimmer
    closed its thinking on the fast pass's own answer."""
    if passes_gate(r, fast, share_min):
        return Outcome("instant", r["forward"]["top"], False, False, 0)
    if skip:
        return Outcome("guessed", r["forward"]["top"], False, False, 0)
    o = loop_end(r, think_line, share_min)
    if o.tier == "guessed" and r["finished"] and o.top == r["forward"]["top"]:
        return Outcome("thought", o.top, True, False, o.think_tokens)
    return o


def right(r: dict) -> int:
    return r["options"].index(r["label"])


# ---- option offsets and temperature ------------------------------------------------------------


OFFSET_L2 = 1.0  # pulls each offset towards 0, so a few labels can't push one far
FLOOR = 1e-9  # a probability the offsets' log loss can take
TEMPERATURES = [round(0.5 * 1.05 ** k, 2) for k in range(62)]  # 0.5 to about 10


def _shifted(p: dict, offsets: Sequence[float]) -> dict:
    share = sum(p["probs"])
    if share <= 0:
        return p
    probs = shift(p["probs"], offsets)
    top = max(range(len(probs)), key=probs.__getitem__)
    return {**p, "probs": probs, "conf": probs[top] / share, "top": top}


def with_offsets(r: dict, offsets: Sequence[float] | None) -> dict:
    """The record with the offsets on its fast-pass probes. Thinking probes stay as recorded: the
    decider applies offsets to the fast pass only."""
    if not offsets:
        return r
    return {**r, "forward": _shifted(r["forward"], offsets), "reversed": _shifted(r["reversed"], offsets)}


def fit_offsets(recs: list[dict], steps: int = 600, rate: float = 1.0) -> tuple[float, ...]:
    """Per-option log offsets that make the shifted forward probe most likely on the labels: a
    softmax over log probability plus offset, L2 towards 0, by gradient descent. Rounded to 3
    decimals, as they go in the lines file."""
    rows = [([math.log(max(p, FLOOR)) for p in r["forward"]["probs"]], right(r)) for r in recs]
    b = [0.0] * len(recs[0]["options"])
    for _ in range(steps):
        grad = [OFFSET_L2 * x for x in b]
        for logs, y in rows:
            z = [v + x for v, x in zip(logs, b)]
            top = max(z)
            e = [math.exp(v - top) for v in z]
            total = sum(e)
            for i, v in enumerate(e):
                grad[i] += v / total - (i == y)
        b = [x - rate * g / len(rows) for x, g in zip(b, grad)]
    center = mean(b)
    return tuple(round(x - center, 3) for x in b)


def folds(xs: list) -> list[tuple[list, list]]:
    """(fit on, held out) for each of FOLDS interleaved folds."""
    return [([x for i, x in enumerate(xs) if i % FOLDS != k], [x for i, x in enumerate(xs) if i % FOLDS == k])
            for k in range(FOLDS)]


def held_out_offsets(recs: list[dict]) -> tuple[int, int]:
    """The fast pass's right answers without offsets, and with each fold's offsets fitted on the
    other folds."""
    plain = sum(r["forward"]["top"] == right(r) for r in recs)
    shifted = sum(with_offsets(r, b)["forward"]["top"] == right(r)
                  for fit_on, out in folds(recs) for b in [fit_offsets(fit_on)] for r in out)
    return plain, shifted


@dataclass(frozen=True)
class Fit:
    recs: list[dict]  # with the offsets applied, when they're kept
    offsets: tuple[float, ...] | None  # in option order; None when not kept
    options: tuple[str, ...]
    held_out: tuple[int, int] | None  # held_out_offsets; None when not tried


def prepare(field: str, recs: list[dict]) -> Fit:
    """Offsets for a field's fast pass, kept when they add OFFSET_MIN_GAIN of its labeled cells in
    right answers held out, then fitted on every record."""
    options = tuple(recs[0]["options"])
    if field not in OFFSET_FIELDS or len(recs) < 2 * FOLDS or any(tuple(r["options"]) != options for r in recs):
        return Fit(recs, None, options, None)
    plain, shifted = held_out_offsets(recs)
    if shifted - plain < OFFSET_MIN_GAIN * len(recs):
        return Fit(recs, None, options, (plain, shifted))
    b = fit_offsets(recs)
    return Fit([with_offsets(r, b) for r in recs], b, options, (plain, shifted))


def final_probs(r: dict, o: Outcome) -> list[float]:
    """The probe a decision's answer comes from: the forward fast-pass probe, or the thinking probe
    the loop stopped on (decider/decide.py, the last trace entry)."""
    if not o.looped:
        return r["forward"]["probs"]
    return next(e["probs"] for e in r["trace"] if e["think_tokens"] == o.think_tokens)


def fit_temperature(pairs: list[tuple[Sequence[float], int]]) -> float:
    """The temperature with the least log loss on the right option. pairs: (probs, right option)."""
    pairs = [(p, y) for p, y in pairs if sum(p) > 0]
    if not pairs:
        return 1.0
    return min(TEMPERATURES, key=lambda t: -sum(math.log(tempered(p, t)[y]) for p, y in pairs))


def fit_temperatures(pairs: list[tuple[Sequence[float], int, bool]]) -> tuple[float, float]:
    """(fast-pass temperature, thinking temperature). pairs: (probs, right option, from thinking)."""
    return (fit_temperature([(p, y) for p, y, thinking in pairs if not thinking]),
            fit_temperature([(p, y) for p, y, thinking in pairs if thinking]))


def ece(pairs: list[tuple[float, bool]]) -> float:
    """Expected calibration error: per BINS bin, |accuracy - mean confidence|, weighted by its count."""
    total = 0.0
    for n, (lo, hi) in enumerate(zip(BINS, BINS[1:])):
        last = n == len(BINS) - 2
        inside = [(c, ok) for c, ok in pairs if lo <= c < hi or (last and c >= hi)]
        if inside:
            total += abs(mean(ok for _, ok in inside) - mean(c for c, _ in inside)) * len(inside)
    return total / len(pairs) if pairs else 0.0


def held_out_ece(pairs: list[tuple[Sequence[float], int, bool]]) -> tuple[float, float]:
    """ECE of the raw confidence, and of the calibrated one with each fold's temperatures fitted on
    the other folds."""
    def scored(ts: tuple[float, float], ps: list[tuple[Sequence[float], int, bool]]) -> list[tuple[float, bool]]:
        return [(calibrated(p, ts[thinking]), max(range(len(p)), key=p.__getitem__) == y)
                for p, y, thinking in ps if sum(p) > 0]

    held = [x for fit_on, out in folds(pairs) for x in scored(fit_temperatures(fit_on), out)]
    return ece(scored((1.0, 1.0), pairs)), ece(held)


def lowest(candidates: list[float], errors_at: Callable[[float], tuple[int, int]], max_error: float) -> float:
    """The lowest candidate line whose accepted fields hold no errors (max_error 0) or at most
    max_error of them. NEVER when no line does."""
    for line in sorted(set(candidates)):
        wrong, accepted = errors_at(line)
        if accepted and (wrong == 0 if max_error == 0 else wrong <= max_error * accepted):
            return line
    return NEVER


def tidy(line: float, outcome: Callable[[float], object]) -> float:
    """The line with as few decimals as keep every labeled outcome the same."""
    if line >= NEVER:
        return NEVER
    want = outcome(line)
    for d in (2, 3, 4, 5, 6):
        short = math.floor(round(line * 10 ** d, 9)) / 10 ** d
        if outcome(short) == want:
            return short
    return line


@dataclass
class FieldPlan:
    field: str
    cost: str
    n: int
    fast: float
    think: float
    fast_forward_only: float  # the same rule for the fast line without the reversed check
    fast_error: float  # forward probe's top option vs the label, over every labeled field
    skip: bool
    think_data: bool  # whether any field was left unsure to calibrate the think line on
    think_fixed: bool = False  # the think line is set by hand (THINK_STOPS)
    think_rule: float = NEVER  # the think line the zero-error or 10% rule gives
    fast_fixed: bool = False  # the fast line is set by hand (FAST_LINES)
    fast_rule: float = NEVER  # the fast line the rule gives
    temperature: float = 1.0  # fitted to the decisions these lines make from the fast pass
    think_temperature: float = 1.0  # and to those from a thinking probe
    ece: tuple[float, float] = (0.0, 0.0)  # raw, and calibrated held out (held_out_ece)
    options: tuple[str, ...] = ()
    offsets: tuple[float, ...] | None = None  # Fit.offsets: the plan's lines are on the shifted probes
    offsets_held_out: tuple[int, int] | None = None


def plan_field(field: str, recs: list[dict], share_min: float) -> FieldPlan:
    cost = COST[field]
    max_error = 0.0 if cost == "expensive" else CHEAP_MAX_ERROR

    def fast_errors(reversed_check: bool) -> Callable[[float], tuple[int, int]]:
        def at(line: float) -> tuple[int, int]:
            acc = [r for r in recs if passes_gate(r, line, share_min, reversed_check)]
            return sum(r["forward"]["top"] != right(r) for r in acc), len(acc)
        return at

    def fast_line(reversed_check: bool) -> float:
        cands = [r["forward"]["conf"] for r in recs if passes_gate(r, 0.0, share_min, reversed_check)]
        line = lowest(cands, fast_errors(reversed_check), max_error)
        return tidy(line, lambda x: frozenset(i for i, r in enumerate(recs)
                                              if passes_gate(r, x, share_min, reversed_check)))

    fast_rule = fast_line(True)
    fast = FAST_LINES.get(field, fast_rule)
    unsure = [r for r in recs if not passes_gate(r, fast, share_min)]

    def think_errors(line: float) -> tuple[int, int]:
        ends = [(loop_end(r, line, share_min), r) for r in unsure]
        thought = [(o, r) for o, r in ends if o.tier == "thought"]
        return sum(o.top != right(r) for o, r in thought), len(thought)

    cands = [e["conf"] for r in unsure for e in r["trace"] if e["share"] >= share_min]
    if cands:
        think_line = tidy(lowest(cands, think_errors, max_error),
                          lambda x: tuple(loop_end(r, x, share_min) for r in unsure))
    else:
        think_line = fast if fast < NEVER else NEVER
    fast_error = sum(r["forward"]["top"] != right(r) for r in recs) / len(recs)
    fixed = field in THINK_STOPS
    # a hand fast line says when to think, so its field doesn't skip thinking by the rule
    skip = field not in FAST_LINES and (field in SKIP_THINKING
                                        or (cost == "cheap" and fast_error <= CHEAP_MAX_ERROR))
    used = THINK_STOPS[field] if fixed else think_line
    outs = [(decide(r, fast, used, skip, share_min), r) for r in recs]
    pairs = [(final_probs(r, o), right(r), o.looped) for o, r in outs]
    return FieldPlan(field, cost, len(recs), fast, used, fast_line(False), fast_error, skip,
                     bool(cands), fixed, think_line, field in FAST_LINES, fast_rule,
                     *fit_temperatures(pairs), held_out_ece(pairs), tuple(recs[0]["options"]))


def plan(field: str, recs: list[dict], share_min: float) -> FieldPlan:
    """Offsets first (prepare), then the lines on the shifted probes."""
    fit = prepare(field, recs)
    return replace(plan_field(field, fit.recs, share_min), offsets=fit.offsets, offsets_held_out=fit.held_out)


def proposal(plans: list[FieldPlan]) -> dict:
    lines = {}
    for p in plans:
        lines[p.field] = {"fast": p.fast, "think": p.think, "temperature": p.temperature,
                          "think_temperature": p.think_temperature}
        if p.offsets:
            lines[p.field]["offsets"] = dict(zip(p.options, p.offsets))
    return {"lines": lines, "skip_thinking": [p.field for p in plans if p.skip]}


# ---- the report -------------------------------------------------------------------------------


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "-"


def _line(x: float) -> str:
    return "never" if x >= NEVER else f"{x:g}"


def histogram(pairs: list[tuple[float, bool]]) -> list[str]:
    """Confidence counts per bin, right answers beside wrong ones."""
    counts = []
    for n, (lo, hi) in enumerate(zip(BINS, BINS[1:])):
        last = n == len(BINS) - 2
        inside = [ok for c, ok in pairs if lo <= c < hi or (last and c >= hi)]
        name = f"<{hi:g}" if n == 0 else (f">={lo:g}" if last else f"{lo:g}-{hi:g}")
        counts.append((name, sum(inside), len(inside) - sum(inside)))
    top = max([max(r, w) for _, r, w in counts] + [1])

    def bar(k: int) -> str:
        return ("#" * math.ceil(BAR * k / top)).ljust(BAR)

    return [f"    {name:>10}  right {bar(r)} {r:>4}   wrong {bar(w)} {w:>4}" for name, r, w in counts]


def field_report(p: FieldPlan, recs: list[dict], share_min: float) -> list[str]:
    """recs with the plan's offsets applied (with_offsets)."""
    out = [f"\n== {p.field} ({p.cost}): {p.n} labeled =="]
    if p.offsets_held_out:
        plain, shifted = p.offsets_held_out
        kept = ("kept: " + ", ".join(f"{v} {b:+.2f}" for v, b in zip(p.options, p.offsets)) if p.offsets
                else f"not kept (they need {OFFSET_MIN_GAIN:.0%} of the cells)")
        out.append(f"  Option offsets, {FOLDS}-fold held out: fast pass right on {plain} -> {shifted} of "
                   f"{p.n}; {kept}.")
    fwd_wrong = sum(r["forward"]["top"] != right(r) for r in recs)
    flips = sum(r["reversed"]["top"] != r["forward"]["top"] for r in recs)
    out.append(f"  Fast pass{' (with offsets)' if p.offsets else ''}: {fwd_wrong} of {p.n} wrong "
               f"({100 * p.fast_error:.1f}% error). The reversed probe picked another option on {flips}.")
    out.append("  Confidence, fast pass (forward probe):")
    out += histogram([(r["forward"]["conf"], r["forward"]["top"] == right(r)) for r in recs])
    out.append("  Confidence after thinking to the cap (the loop's last probe):")
    out += histogram([(r["trace"][-1]["conf"], r["trace"][-1]["top"] == right(r)) for r in recs])
    capped = sum(not r["finished"] for r in recs)
    tokens = [r["think_tokens"] for r in recs]
    out.append(f"  Thinking with no line: {capped} of {p.n} hit the cap ({_pct(capped, p.n)}), the rest "
               f"closed their thinking; median {median(tokens):.0f} tokens, max {max(tokens)}.")

    rule = "zero labeled errors" if p.cost == "expensive" else "at most 10% errors"
    instant = [r for r in recs if passes_gate(r, p.fast, share_min)]
    alone = [r for r in recs if passes_gate(r, p.fast_forward_only, share_min, False)]
    if p.fast_fixed:
        out.append(f"  Fast line set by hand (FAST_LINES): {_line(p.fast)}, instant on {len(instant)} of {p.n} "
                   f"({_pct(len(instant), p.n)}). The rule ({rule}) gives {_line(p.fast_rule)}.")
    else:
        out.append(f"  Proposed fast line ({rule}): {_line(p.fast)}, instant on {len(instant)} of {p.n} "
                   f"({_pct(len(instant), p.n)}). Without the reversed check the line would be "
                   f"{_line(p.fast_forward_only)}, instant on {len(alone)} ({_pct(len(alone), p.n)}).")
    think_note = "" if p.think_data else " (nothing left unsure to calibrate it on)"
    skip_note = ("" if not p.skip else " In skip_thinking by hand (SKIP_THINKING)." if p.field in SKIP_THINKING
                 else " In skip_thinking: its fast-pass error is 10% or less.")
    if p.think_fixed:
        out.append(f"  Think line set by hand (THINK_STOPS): {_line(p.think)}. The rule ({rule}) "
                   f"gives {_line(p.think_rule)}{think_note}.{skip_note}")
    else:
        out.append(f"  Proposed think line ({rule}): {_line(p.think)}{think_note}.{skip_note}")

    outs = [(decide(r, p.fast, p.think, p.skip, share_min), r) for r in recs]
    tiers = {}
    for tier in ("instant", "thought", "guessed"):
        these = [(o, r) for o, r in outs if o.tier == tier]
        ok = sum(o.top == right(r) for o, r in these)
        tiers[tier] = f"{tier} {len(these)} ({_pct(len(these), p.n)}, {_pct(ok, len(these))} right)"
    ok = sum(o.top == right(r) for o, r in outs)
    loops = [o for o, _ in outs if o.looped]
    caps = sum(o.cap_hit for o in loops)
    near = (f" Within one level {_pct(sum(abs(o.top - right(r)) <= 1 for o, r in outs), p.n)}."
            if p.field in ORDINAL else "")
    out.append(f"  Under the proposal: {', '.join(tiers.values())}. Accuracy {_pct(ok, p.n)}.{near} "
               f"{len(loops)} thinking loops, {caps} hit the cap ({_pct(caps, len(loops))}), "
               f"median {median([o.think_tokens for o in loops]) if loops else 0:.0f} tokens.")
    raw, cal = p.ece
    out.append(f"  Temperatures fitted to these decisions: {p.temperature:g} from the fast pass, "
               f"{p.think_temperature:g} from thinking. Calibration error (ECE) {raw:.3f} on the raw "
               f"confidence, {cal:.3f} calibrated ({FOLDS}-fold held out).")
    if p.field in CLARIFY_FIELDS:
        asked = [(o, r) for o, r in outs if o.tier == "guessed"]
        wrong = sum(o.top != right(r) for o, r in asked)
        errors = p.n - ok
        out.append(f"  To clarify (thinking ended off the fast pass's answer, or at the cap): {len(asked)} of "
                   f"{p.n} ({_pct(len(asked), p.n)}), {wrong} of them wrong ({_pct(wrong, len(asked))}). "
                   f"That puts {wrong} of the {errors} errors in front of the student.")
    return out


def default_report(defaults: list[tuple[str, str, str]], asked: list[tuple[Outcome, dict]]) -> list[str]:
    """Templates the type settles: (item key, the default, the label) for each, beside the asked ones."""
    if not defaults:
        return []
    ok = sum(d == label for _, d, label in defaults)
    asked_ok = sum(o.top == right(r) for o, r in asked)
    out = [f"  From the type (test, problem set or lab, so no template question): {len(defaults)}, "
           f"{ok} right ({_pct(ok, len(defaults))}). With them, template accuracy is "
           f"{_pct(ok + asked_ok, len(defaults) + len(asked))} of {len(defaults) + len(asked)}."]
    out += [f"    {key}: {d}, labeled {label}" for key, d, label in defaults if d != label]
    return out


def skipped_report(items: list[Item], skipped: list[Skipped]) -> list[str]:
    kinds = {}
    for i in items:
        kinds[i.kind] = kinds.get(i.kind, 0) + 1
    out = ["Labeled set: " + ", ".join(f"{n} {k.replace('_', ' ')}s" for k, n in kinds.items())
           + f", {sum(len(i.labels) for i in items)} labeled fields."]
    if not skipped:
        return out + ["Skipped labels: none."]
    out.append(f"Skipped labels: {len(skipped)}.")
    by_reason: dict[str, list[Skipped]] = {}
    for s in skipped:
        by_reason.setdefault(s.reason, []).append(s)
    for reason, rows in by_reason.items():
        keys = sorted({s.key for s in rows})
        if reason == "not a current option":
            out.append(f"  Not a current option ({len(rows)}):")
            out += [f"    {s.key} {s.field} = {s.label!r}" for s in rows]
        else:
            out.append(f"  {reason}: {len(rows)} labels on {len(keys)} items")
    return out


def without_offsets(field: str, recs: list[dict], share_min: float) -> str:
    p = plan_field(field, recs, share_min)
    outs = [decide(r, p.fast, p.think, p.skip, share_min) for r in recs]
    instant = sum(o.tier == "instant" for o in outs)
    ok = sum(o.top == right(r) for o, r in zip(outs, recs))
    return (f"  Without offsets the rule's lines would be fast {_line(p.fast)}, think {_line(p.think)}: instant "
            f"{instant} ({_pct(instant, p.n)}), accuracy {_pct(ok, p.n)}, "
            f"{sum(o.looped for o in outs)} thinking loops.")


def chain_report(plans: list[FieldPlan], by_field: dict[str, list[dict]], share_min: float) -> list[str]:
    """Joint mode: one chain per assignment, as long as its longest field's loop, against the same
    loops run one per field."""
    loops: dict[str, list[int]] = {}
    for p in plans:
        for r in by_field[p.field]:
            o = decide(with_offsets(r, p.offsets), p.fast, p.think, p.skip, share_min)
            if o.looped:
                loops.setdefault(r["key"], []).append(o.think_tokens)
    if not loops:
        return []
    chains = [max(ts) for ts in loops.values()]
    apart = [sum(ts) for ts in loops.values()]
    return [f"\nShared chain under the proposal: {len(loops)} assignments think, median chain {median(chains):.0f} "
            f"tokens (max {max(chains)}). The same loops one per field would be median {median(apart):.0f} "
            f"tokens (max {max(apart)}). Labeled fields only; the decider also probes unlabeled ones."]


def report(items: list[Item], skipped: list[Skipped], share_min: float,
           template_from_type: bool = True, mode: str = "fields") -> tuple[str, dict, list[FieldPlan]]:
    """The printed report, the proposed lines file and the per-field plans, from the records.
    With template_from_type, a template the type settles is scored as that default, not as a record.
    Joint mode covers assignments only."""
    if mode == "joint":
        items = [i for i in items if i.kind == ASSIGNMENT_KIND]
    recs = load_records(mode)
    answers = type_answers(items, recs, None, share_min)
    by_field: dict[str, list[dict]] = {}
    defaults: list[tuple[str, str, str]] = []
    missing = 0
    for item in items:
        for f in item.fields:
            if f.key not in item.labels:
                continue
            if f.key == "template" and (d := from_type(template_from_type, answers.get(item.key))):
                defaults.append((item.key, d, item.labels[f.key]))
                continue
            r = recs.get((item.key, f.key))
            if r is None or item.labels[f.key] not in r["options"]:
                missing += 1
                continue
            by_field.setdefault(f.key, []).append({**r, "label": item.labels[f.key]})

    out = skipped_report(items, skipped)
    if missing:
        out.append(f"Not recorded yet: {missing} labeled fields (run calibrate again to record them).")
    plans = []
    for f in ASSIGNMENT_FIELDS + (() if mode == "joint" else QUESTION_FIELDS):
        if f.key in by_field:
            p = plan(f.key, by_field[f.key], share_min)
            plans.append(p)
            shifted = [with_offsets(r, p.offsets) for r in by_field[f.key]]
            out += field_report(p, shifted, share_min)
            if p.offsets:
                out.append(without_offsets(f.key, by_field[f.key], share_min))
            if f.key == "template":
                out += default_report(defaults, [(decide(r, p.fast, p.think, p.skip, share_min), r)
                                                 for r in shifted])
        else:
            out.append(f"\n== {f.key} ({COST[f.key]}): no records, so no line (the decider keeps 0.85) ==")
    if mode == "joint":
        out += chain_report(plans, by_field, share_min)
    prop = proposal(plans)
    out.append(f"\nProposal for {'decisions/lines.joint.json' if mode == 'joint' else 'decisions/lines.json'}:")
    out.append(f"  {'field':<10}{'cost':<11}{'fast':>8}{'think':>8}{'fast error':>12}{'temps':>12}  "
               f"{'offsets':<8} skip_thinking")
    for p in plans:
        temps = f"{p.temperature:g}/{p.think_temperature:g}"
        out.append(f"  {p.field:<10}{p.cost:<11}{_line(p.fast):>8}{_line(p.think):>8}"
                   f"{100 * p.fast_error:>11.1f}%{temps:>12}  {'yes' if p.offsets else 'no':<8} "
                   f"{'yes' if p.skip else 'no'}")
    out.append(json.dumps(prop, indent=2))
    return "\n".join(out), prop, plans
