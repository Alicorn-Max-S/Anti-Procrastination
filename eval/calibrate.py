"""calibrate (spec, Part C): System One over every labeled item with no gate, then proposed lines.

For every item and field it records the forward fast-pass probe, the reversed probe and a full
thinking loop up to the cap, with the probe after every paragraph. The loop's think line is out of
reach, so it only ends when Glimmer closes its thinking or the cap is hit. The decider's loop runs
the same prompts with the same seed and stops at the first probe that clears its line, so any pair
of lines can be replayed from these records without another model call.

Proposed lines:
- expensive fields: the lowest line that accepts zero labeled errors;
- cheap fields: the lowest line that accepts no more than 10% errors;
- skip_thinking: every cheap field whose fast-pass error is 10% or less, plus SKIP_THINKING.
The fast line is scored on what the gate acts on (the forward probe clears it and the reversed probe
agrees); the think line is replayed on the fields the proposed fast line leaves unsure.
"""

import json
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from dataclasses import dataclass
from statistics import median

from decider.config import Settings
from decider.llama import Llama
from decider.probe import Probe, probe
from decider.prompts import TEMPLATE_FROM_TYPE, USES_TYPE, Prompt, choice_for, with_type
from decider.think import think
from decisions.fields import ASSIGNMENT_FIELDS, ASSIGNMENT_KIND, QUESTION_FIELDS, Field

from .items import EVAL_DATA, Item, Skipped

RECORDS = EVAL_DATA / "calibrate.jsonl"
COST = {"type": "expensive", "template": "expensive", "effort": "cheap",
        "format": "cheap", "topic": "expensive", "credit": "expensive"}  # spec, Fields
CHEAP_MAX_ERROR = 0.10
# Think lines set by hand instead of by the zero-error rule. Credit stops thinking once a probe is
# this sure: on the v8 records it keeps the same 24 of 26 right and cuts about a third of the
# thinking. The same stop costs type and template 4 or 5 right answers each, so they stay at never.
THINK_STOPS = {"credit": 0.999}
# Cheap fields that skip thinking by hand, whatever their fast-pass error. Effort: Max, Nov 1. On the
# v9 records its thinking costs about 12 s per assignment and gains 5 right answers of 191.
SKIP_THINKING = {"effort"}
CLARIFY_FIELDS = ("type", "template")  # decider/decide.py: a guessed answer goes to the student
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


def load_records() -> dict[tuple[str, str], dict]:
    if not RECORDS.exists():
        return {}
    out = {}
    for line in RECORDS.read_text(encoding="utf-8").splitlines():
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
    p = plan_field("type", labeled, share_min)
    return {k: r["options"][decide(r, p.fast, p.think, p.skip, share_min).top] for k, r in have.items()}


def record_all(llama: Llama, s: Settings, items: list[Item], seed: int, fresh: bool = False,
               slots: int = 1) -> None:
    """Records every labeled field that has no record under this run yet, appending as it goes.

    With slots > 1 (llama-server started with -np N), N items are recorded at once, each on its own
    slot, so one item's fields still share their cached prefix. Batched decoding is several times
    faster in total. Its logits can differ from a single slot's in the last bits, so now and then a
    sampled thinking token differs from what the decider's one slot would sample.

    Two passes, as the decider goes: type for every assignment first (labeled or not, since template
    and effort need System One's answer), then every other labeled field."""
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
                 slots: int) -> None:
    """Records each item's fields on one slot, N items at once, appending as it goes."""
    total = sum(len(fs) for _, fs, _ in todo)
    if not total:
        return
    RECORDS.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    lock = threading.Lock()
    free: Queue = Queue()
    for slot in range(slots):
        free.put(slot)
    done = 0

    with RECORDS.open("a", encoding="utf-8") as out:
        def one_item(job: tuple[Item, list[Field], str | None]) -> None:
            nonlocal done
            item, fields, type_answer = job
            slot = free.get()
            try:
                for f in fields:
                    r = record(llama, s, item, f, seed, slot, type_answer if f.key in USES_TYPE else None)
                    fwd, last = r["forward"], r["trace"][-1]
                    right = r["options"].index(r["label"]) if r["label"] in r["options"] else None

                    def mark(top: int) -> str:
                        return "unlabeled" if right is None else "ok" if top == right else "WRONG"

                    with lock:
                        out.write(json.dumps(r) + "\n")
                        out.flush()
                        done += 1
                        eta = (time.perf_counter() - start) / done * (total - done) / 60
                        print(f"  [{done}/{total}] {item.key} {f.key}: fast {fwd['conf']:.3f} "
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

    fast = fast_line(True)
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
    return FieldPlan(field, cost, len(recs), fast, THINK_STOPS[field] if fixed else think_line,
                     fast_line(False), fast_error,
                     cost == "cheap" and (fast_error <= CHEAP_MAX_ERROR or field in SKIP_THINKING),
                     bool(cands), fixed, think_line)


def proposal(plans: list[FieldPlan]) -> dict:
    return {"lines": {p.field: {"fast": p.fast, "think": p.think} for p in plans},
            "skip_thinking": [p.field for p in plans if p.skip]}


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
    out = [f"\n== {p.field} ({p.cost}): {p.n} labeled =="]
    fwd_wrong = sum(r["forward"]["top"] != right(r) for r in recs)
    flips = sum(r["reversed"]["top"] != r["forward"]["top"] for r in recs)
    out.append(f"  Fast pass: {fwd_wrong} of {p.n} wrong ({100 * p.fast_error:.1f}% error). The reversed "
               f"probe picked another option on {flips}.")
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
    out.append(f"  Proposed fast line ({rule}): {_line(p.fast)}, instant on {len(instant)} of {p.n} "
               f"({_pct(len(instant), p.n)}). Without the reversed check the line would be "
               f"{_line(p.fast_forward_only)}, instant on {len(alone)} ({_pct(len(alone), p.n)}).")
    think_note = "" if p.think_data else " (nothing left unsure to calibrate it on)"
    if p.think_fixed:
        out.append(f"  Think line set by hand (THINK_STOPS): {_line(p.think)}. The rule ({rule}) "
                   f"gives {_line(p.think_rule)}{think_note}.")
    else:
        out.append(f"  Proposed think line ({rule}): {_line(p.think)}{think_note}."
                   + ("" if not p.skip else " In skip_thinking: its fast-pass error is 10% or less."
                      if p.fast_error <= CHEAP_MAX_ERROR else " In skip_thinking by hand (SKIP_THINKING)."))

    outs = [(decide(r, p.fast, p.think, p.skip, share_min), r) for r in recs]
    tiers = {}
    for tier in ("instant", "thought", "guessed"):
        these = [(o, r) for o, r in outs if o.tier == tier]
        ok = sum(o.top == right(r) for o, r in these)
        tiers[tier] = f"{tier} {len(these)} ({_pct(len(these), p.n)}, {_pct(ok, len(these))} right)"
    ok = sum(o.top == right(r) for o, r in outs)
    loops = [o for o, _ in outs if o.looped]
    caps = sum(o.cap_hit for o in loops)
    out.append(f"  Under the proposal: {', '.join(tiers.values())}. Accuracy {_pct(ok, p.n)}. "
               f"{len(loops)} thinking loops, {caps} hit the cap ({_pct(caps, len(loops))}), "
               f"median {median([o.think_tokens for o in loops]) if loops else 0:.0f} tokens.")
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


def report(items: list[Item], skipped: list[Skipped], share_min: float,
           template_from_type: bool = True) -> tuple[str, dict, list[FieldPlan]]:
    """The printed report, the proposed lines file and the per-field plans, from the records.
    With template_from_type, a template the type settles is scored as that default, not as a record."""
    recs = load_records()
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
    for f in ASSIGNMENT_FIELDS + QUESTION_FIELDS:
        if f.key in by_field:
            p = plan_field(f.key, by_field[f.key], share_min)
            plans.append(p)
            out += field_report(p, by_field[f.key], share_min)
            if f.key == "template":
                out += default_report(defaults, [(decide(r, p.fast, p.think, p.skip, share_min), r)
                                                 for r in by_field[f.key]])
        else:
            out.append(f"\n== {f.key} ({COST[f.key]}): no records, so no line (the decider keeps 0.85) ==")
    prop = proposal(plans)
    out.append("\nProposal for decisions/lines.json:")
    out.append(f"  {'field':<10}{'cost':<11}{'fast':>8}{'think':>8}{'fast error':>12}  skip_thinking")
    for p in plans:
        out.append(f"  {p.field:<10}{p.cost:<11}{_line(p.fast):>8}{_line(p.think):>8}"
                   f"{100 * p.fast_error:>11.1f}%  {'yes' if p.skip else 'no'}")
    out.append(json.dumps(prop, indent=2))
    return "\n".join(out), prop, plans
