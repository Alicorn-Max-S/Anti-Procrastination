"""compare (spec, Part C): six setups on the labeled set, scored against Max's labels.

1. All Spark: Muse Spark with structured output, one call per item covering all of its fields.
   Cost comes from the usage the API returns, at Standard prices.
2. Base Glimmer: /v1/chat/completions at reasoning strength low, one call per item covering all of
   its fields, with the answers parsed from the text.
3. System One, fast pass only: the forward fast-pass probe's answer for every field, timed by the
   fast pass alone. Taken from setup 4's records.
4. System One, full: the decider with the lines file (decisions/lines.json unless LINES_FILE says otherwise).
5. System One, shared chain: the decider in joint mode (decider/joint.py) with
   decisions/lines.joint.json: an assignment's fields in one prompt and one thinking chain. Test
   questions are decided per field, as in setup 4.
6. Liquid d1-2B: a 2B decision model on its own llama-server (D1_URL), every field of an item as a
   choice question in one /v1/systemone call. It never thinks and has no letters: each option is a
   named criterion, and the question carries the field's rules and worked examples, which the other
   setups get in their system turn.

Every setup sees the same item text (Item.context) and the same questions, options and letters
(decider.prompts.choice_for, built from decisions/fields.py). Setups 1 and 2 get all of an item's
questions in one message, each question block exactly as the decider asks it; only the closing
line that says how to reply differs. A graded test's questions go to the decider together, as in
Part B, so each question is charged the test's wall time divided by its questions.

Effort's options are in order of size, so it's also scored within one level (calibrate.ORDINAL).
eval/audit.md lists the labels Spark, base Glimmer and System One all miss, to check by hand.

Per-item records stay in data/eval/compare/<setup>.jsonl; the outputs hold aggregates only.
"""

import csv
import hashlib
import json
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from statistics import mean

import httpx

from decider.config import Settings, settings
from decider.decide import Decider
from decider.joint import MULTI_SYSTEM, questions_text, user_text
from decider.llama import Llama
from decider.prompts import FIELDS, LETTERS, Choice, choice_for, option_text
from decisions.fields import ASSIGNMENT_FIELDS, ASSIGNMENT_KIND, QUESTION_FIELDS

from .calibrate import COST, ORDINAL
from .items import EVAL_DATA, ROOT, Item, Skipped

OUT = ROOT / "eval"
RECORDS = EVAL_DATA / "compare"
RUNNABLE = ("spark", "glimmer", "system_one", "joint", "d1")
DECIDERS = ("system_one", "joint")  # setups that run the decider, and so have tiers
TIERS = ("instant", "thought", "guessed", "default")  # decider/decide.py
SETUPS = {  # in chart and table order
    "spark": "All Spark",
    "glimmer": "Base Glimmer",
    "fast": "System One, fast pass only",
    "system_one": "System One, full",
    "joint": "System One, shared chain",
    "d1": "Liquid d1-2B",
}
SPARK_PRICES = {"input": 1.25, "cached": 0.15, "output": 4.25}  # $ per million tokens, Standard mode
GPU_HOUR = 1.10  # $ per hour: Modal's A10, as a rental proxy for Glimmer's GPU time
GLIMMER_MAX_TOKENS = 4096
RETRIES = 3
D1_URL = os.getenv("D1_URL", "http://127.0.0.1:8081").rstrip("/")
SPARK_INSTRUCTIONS = MULTI_SYSTEM.split("\n\nReasoning strength:")[0]  # that line is Glimmer's


def choices(item: Item) -> list[Choice]:
    return [choice_for(f, item.topics) for f in item.fields]


def letter_value(c: Choice, letter: str | None) -> str | None:
    if not letter or letter not in LETTERS[:len(c.order)]:
        return None
    return c.options[c.order[LETTERS.index(letter)]].value


# ---- setup 1: all Spark -----------------------------------------------------------------------


def spark_cost(usage: dict) -> float:
    inp = usage.get("input_tokens", 0)
    cached = (usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    out = usage.get("output_tokens", 0)
    p = SPARK_PRICES
    return ((inp - cached) * p["input"] + cached * p["cached"] + out * p["output"]) / 1e6


def spark_item(client: httpx.Client, item: Item) -> dict:
    from app import meta_client
    from app.config import settings as app_settings

    cs = choices(item)
    text = (f"{item.context()}\n\n{questions_text(cs)}\n\n"
            "Answer every question with the letter of its best option.")
    schema = {
        "type": "object",
        "properties": {c.field: {"type": "string", "enum": list(LETTERS[:len(c.order)])} for c in cs},
        "required": [c.field for c in cs],
        "additionalProperties": False,
    }
    body = {
        "model": app_settings.meta_model,
        "instructions": SPARK_INSTRUCTIONS,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
        "text": {"format": {"type": "json_schema", "name": "Decisions", "schema": schema, "strict": True}},
        "store": False,  # real school data: don't keep the response on Meta's side
    }
    for attempt in range(RETRIES):
        start = time.perf_counter()
        try:
            resp = meta_client._post(client, body)
            seconds = time.perf_counter() - start
            reply = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", meta_client.output_text(resp).strip()))
            break
        except (meta_client.MetaError, httpx.HTTPError, json.JSONDecodeError) as e:
            if attempt == RETRIES - 1:
                raise
            print(f"    retrying {item.key}: {e}", flush=True)
            time.sleep(2 ** (attempt + 1))
    usage = resp.get("usage") or {}
    return {
        "answers": {c.field: letter_value(c, reply.get(c.field)) for c in cs},
        "seconds": round(seconds, 3),
        "usage": usage,
        "cost": spark_cost(usage),
        "retries": attempt,
    }


# ---- setup 2: base Glimmer --------------------------------------------------------------------


def parse_letters(text: str, cs: list[Choice]) -> dict[str, str | None]:
    """Each field's letter from lines like "type: B" (the first such line per field)."""
    out = {}
    for c in cs:
        m = re.search(rf"^\W*{re.escape(c.field)}\W*?[:=\-]\s*\(?([A-Z])\b", text, re.M | re.I)
        out[c.field] = letter_value(c, m.group(1).upper() if m else None)
    return out


def glimmer_item(llama: Llama, item: Item, seed: int) -> dict:
    cs = choices(item)
    body = {
        "model": llama.model(),
        "messages": [
            {"role": "system", "content": MULTI_SYSTEM},
            {"role": "user", "content": user_text(item.context(), cs)},
        ],
        "max_tokens": GLIMMER_MAX_TOKENS,
        "chat_template_kwargs": {"reasoning_strength": "low"},
        "temperature": 1.0, "top_p": 0.95, "top_k": 64, "min_p": 0.0,  # the thinking loop's sampler
        "seed": seed,
    }
    start = time.perf_counter()
    r = llama.http.post(llama.url + "/v1/chat/completions", json=body)
    seconds = time.perf_counter() - start
    r.raise_for_status()
    resp = r.json()
    choice = resp["choices"][0]
    content = choice["message"].get("content") or ""
    return {
        "answers": parse_letters(content, cs),
        "seconds": round(seconds, 3),
        "content": content,
        "reasoning_chars": len(choice["message"].get("reasoning_content") or ""),
        "completion_tokens": (resp.get("usage") or {}).get("completion_tokens", 0),
        "finish_reason": choice.get("finish_reason"),
    }


# ---- setup 6: Liquid d1-2B --------------------------------------------------------------------


def d1_question(c: Choice) -> dict:
    """A field as a d1 choice question: the question, then the field's rules and worked examples (the
    other setups see those in their system turn); each option a criterion named by its value."""
    f = FIELDS[c.field]
    lines = [c.question] + [f"Rule: {r}" for r in f.rules]
    if f.examples:
        label = {o.value: o.label for o in c.options}
        lines += ["Worked examples (the item, then the answer):"]
        lines += [f"- {e.item} → {label.get(e.answer, e.answer)}" for e in f.examples]
    return {"type": "choice", "instructions": "\n".join(lines),
            "criteria": {c.options[i].value: option_text(c.options[i]) for i in c.order}}


def d1_item(http: httpx.Client, item: Item) -> dict:
    cs = choices(item)
    body = {"state": item.context(), "questions": {c.field: d1_question(c) for c in cs}}
    start = time.perf_counter()
    r = http.post(D1_URL + "/v1/systemone", json=body)
    seconds = time.perf_counter() - start
    r.raise_for_status()
    got = r.json()["answers"]
    values = {c.field: {o.value for o in c.options} for c in cs}
    return {
        "answers": {f: a.get("choice") if a.get("choice") in values[f] else None for f, a in got.items()},
        "probabilities": {f: a.get("probabilities") for f, a in got.items()},
        "seconds": round(seconds, 3),
    }


def d1_model(http: httpx.Client) -> str:
    props = http.get(D1_URL + "/props", timeout=10).json()
    return os.path.splitext(os.path.basename(props.get("model_alias") or props.get("model_path") or "d1"))[0]


# ---- setups 3, 4 and 5: System One ------------------------------------------------------------


class TimedDecider(Decider):
    """The decider, also counting the time its fast passes take (setup 3 is the fast pass alone)."""

    fast_seconds = 0.0

    def _settle(self, job, lines, slot=0):
        start = time.perf_counter()
        try:
            return super()._settle(job, lines, slot)
        finally:
            self.fast_seconds += time.perf_counter() - start


def _fast_value(d: dict) -> str:
    if not d["trace"]:  # a template the type settled: the fast pass takes the same shortcut
        return d["value"]
    probs = d["trace"][0]["probs"]  # the forward fast-pass probe
    return max(probs, key=probs.get)


def _system_one_record(ds: dict, seconds: float, fast_seconds: float, s: Settings) -> dict:
    return {
        "answers": {f: d["value"] for f, d in ds.items()},
        "fast_answers": {f: _fast_value(d) for f, d in ds.items()},
        "tiers": {f: d["tier"] for f, d in ds.items()},
        "think_tokens": {f: d["think_tokens"] for f, d in ds.items()},
        "looped": {f: len(d["trace"]) > 1 for f, d in ds.items()},
        "cap_hits": {f: d["tier"] == "guessed" and d["think_tokens"] >= s.cap_tokens for f, d in ds.items()},
        "clarify": {f: d.get("clarify", False) for f, d in ds.items()},
        "seconds": round(seconds, 3),
        "fast_seconds": round(fast_seconds, 3),
    }


def system_one_group(decider: TimedDecider, group: list[Item], seed: int) -> list[dict]:
    """One assignment, or one graded test's questions decided together."""
    decider.fast_seconds = 0.0
    start = time.perf_counter()
    first = group[0]
    if first.kind == ASSIGNMENT_KIND:
        out = decider.decide_assignment(first.data, seed=seed)
        per = {first.key: out["decisions"]}
    else:
        out = decider.decide_test(first.topics, [i.data for i in group], course=first.course, seed=seed)
        per = {i.key: out["decisions"][i.data["id"]] for i in group}
    wall = time.perf_counter() - start
    n = len(group)
    return [{**_system_one_record(per[i.key], wall / n, decider.fast_seconds / n, decider.s),
             "timings": out["timings"], "new_topics": out.get("new_topics", [])} for i in group]


# ---- running --------------------------------------------------------------------------------


def lines_hash(mode: str = "fields") -> str:
    from eval.run import lines_path

    return hashlib.sha256(lines_path(mode).read_bytes()).hexdigest()[:12]


def run_id(setup: str, llama: Llama | None, seed: int, d1: httpx.Client | None = None) -> dict:
    if setup == "spark":
        from app.config import settings as app_settings

        return {"model": app_settings.meta_model, "prompt_version": settings.prompt_version}
    if setup == "d1":
        return {"model": d1_model(d1), "prompt_version": settings.prompt_version}
    rid = {"model": llama.model(), "seed": seed, "prompt_version": settings.prompt_version}
    if setup in DECIDERS:
        rid |= {"lines": lines_hash(), "cap": settings.cap_tokens, "slots": settings.parallel_slots}
    if setup == "joint":  # test questions still use lines.json
        rid |= {"joint_lines": lines_hash("joint"), "mode": "joint"}
    return rid


def load_records(setup: str) -> dict[str, dict]:
    path = RECORDS / f"{setup}.jsonl"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        out[r["key"]] = r
    return out


def groups(items: list[Item]) -> list[list[Item]]:
    """Each assignment alone; each test's questions together, in order."""
    out: list[list[Item]] = []
    tests: dict[str, list[Item]] = {}
    for i in items:
        if i.kind == ASSIGNMENT_KIND:
            out.append([i])
        else:
            if i.test not in tests:
                tests[i.test] = []
                out.append(tests[i.test])
            tests[i.test].append(i)
    return out


def run_setups(llama: Llama | None, items: list[Item], setups: Iterable[str], seed: int,
               fresh: bool = False) -> None:
    RECORDS.mkdir(parents=True, exist_ok=True)
    for setup in [s for s in RUNNABLE if s in setups]:
        client = None
        if setup == "spark":
            from app import meta_client

            client = meta_client._client()
        elif setup == "d1":
            client = httpx.Client(timeout=120)
        rid = run_id(setup, llama, seed, client)
        have = {} if fresh else {k: r for k, r in load_records(setup).items() if r["run"] == rid}
        todo = [g for g in groups(items) if setup not in DECIDERS or any(i.key not in have for i in g)]
        if setup not in DECIDERS:
            todo = [[i] for g in todo for i in g if i.key not in have]
        print(f"compare {setup}: {sum(len(g) for g in todo)} items to run", flush=True)
        decider = None
        if setup in DECIDERS:
            decider = TimedDecider(llama, replace(settings, mode="joint" if setup == "joint" else "fields"))
        start = time.perf_counter()
        try:
            with (RECORDS / f"{setup}.jsonl").open("a", encoding="utf-8") as out:
                for n, group in enumerate(todo, 1):
                    if setup == "spark":
                        recs = [spark_item(client, group[0])]
                    elif setup == "glimmer":
                        recs = [glimmer_item(llama, group[0], seed)]
                    elif setup == "d1":
                        recs = [d1_item(client, group[0])]
                    else:
                        recs = system_one_group(decider, group, seed)
                    for item, r in zip(group, recs):
                        out.write(json.dumps({"key": item.key, "kind": item.kind, "run": rid, **r}) + "\n")
                    out.flush()
                    right = sum(r["answers"].get(f) == v for i, r in zip(group, recs) for f, v in i.labels.items())
                    total = sum(len(i.labels) for i in group)
                    elapsed = time.perf_counter() - start
                    print(f"  [{n}/{len(todo)}] {group[0].key}{f' (+{len(group) - 1})' if len(group) > 1 else ''}: "
                          f"{right}/{total} right, {sum(r['seconds'] for r in recs):.1f} s "
                          f"(eta {elapsed / n * (len(todo) - n) / 60:.0f} min)", flush=True)
        finally:
            if client is not None:
                client.close()


# ---- scoring --------------------------------------------------------------------------------


@dataclass
class Score:
    setup: str
    items: int = 0
    seconds: list[float] = field(default_factory=list)
    seconds_by_kind: dict[str, list[float]] = field(default_factory=dict)
    cost: float = 0.0  # Spark's dollars (Glimmer's comes from the seconds)
    usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "cached": 0, "output": 0, "reasoning": 0})
    fields: dict[str, list[bool]] = field(default_factory=dict)  # field -> right per labeled field
    tiers: dict[str, dict[str, int]] = field(default_factory=dict)
    loops: dict[str, int] = field(default_factory=dict)
    cap_hits: dict[str, int] = field(default_factory=dict)
    clarify: dict[str, list[bool]] = field(default_factory=dict)  # field -> right, per field sent to clarify
    near: dict[str, list[bool]] = field(default_factory=dict)  # ORDINAL field -> within one level
    unparsed: int = 0
    truncated: int = 0

    @property
    def sec_per_item(self) -> float:
        return mean(self.seconds) if self.seconds else 0.0

    @property
    def cost_per_1000(self) -> float:
        if self.setup == "spark":
            return self.cost / self.items * 1000 if self.items else 0.0
        return self.sec_per_item * 1000 / 3600 * GPU_HOUR

    def accuracy(self, names: Iterable[str]) -> tuple[int, int]:
        marks = [ok for n in names for ok in self.fields.get(n, [])]
        return sum(marks), len(marks)


def within_one(f: str, answer: str | None, label: str) -> bool:
    """For an ORDINAL field: the answer is the label or a neighbour of it."""
    order = [o.value for o in FIELDS[f].options]
    return answer in order and abs(order.index(answer) - order.index(label)) <= 1


def score(setup: str, items: list[Item], recs: dict[str, dict]) -> Score:
    sc = Score(setup)
    for item in items:
        r = recs.get(item.key)
        if r is None:
            continue
        answers = r["fast_answers"] if setup == "fast" else r["answers"]
        seconds = r["fast_seconds"] if setup == "fast" else r["seconds"]
        sc.items += 1
        sc.seconds.append(seconds)
        sc.seconds_by_kind.setdefault(item.kind, []).append(seconds)
        for f, label in item.labels.items():
            sc.fields.setdefault(f, []).append(answers.get(f) == label)
            if f in ORDINAL:
                sc.near.setdefault(f, []).append(within_one(f, answers.get(f), label))
            if setup in DECIDERS:
                tiers = sc.tiers.setdefault(f, dict.fromkeys(TIERS, 0))
                tiers[r["tiers"][f]] += 1
                if r.get("clarify", {}).get(f):
                    sc.clarify.setdefault(f, []).append(answers.get(f) == label)
                sc.loops[f] = sc.loops.get(f, 0) + r["looped"][f]
                sc.cap_hits[f] = sc.cap_hits.get(f, 0) + r["cap_hits"][f]
        if setup == "spark":
            sc.cost += r["cost"]
            u = r["usage"]
            sc.usage["input"] += u.get("input_tokens", 0)
            sc.usage["cached"] += (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
            sc.usage["output"] += u.get("output_tokens", 0)
            sc.usage["reasoning"] += (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0
        if setup in ("glimmer", "d1"):
            sc.unparsed += sum(v is None for v in r["answers"].values())
        if setup == "glimmer":
            sc.truncated += r["finish_reason"] == "length"
    return sc


# ---- outputs --------------------------------------------------------------------------------

FIELD_ORDER = [f.key for f in ASSIGNMENT_FIELDS + QUESTION_FIELDS]
GROUPS = {"all": FIELD_ORDER, "expensive": [f for f in FIELD_ORDER if COST[f] == "expensive"],
          "cheap": [f for f in FIELD_ORDER if COST[f] == "cheap"]}


def _pct(ok: int, n: int) -> str:
    return f"{100 * ok / n:.1f}%" if n else "-"


def _money(x: float) -> str:
    return f"${x:.2f}" if x >= 0.1 else f"${x:.3f}"


def write_csv(scores: list[Score]) -> None:
    cols = ["setup", "scope", "items", "fields", "correct", "accuracy", "seconds_per_item",
            "cost_per_1000_items", *TIERS, "loops", "cap_hits", "clarify", "clarify_wrong"]
    with (OUT / "results.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for sc in scores:
            name = SETUPS[sc.setup]
            for scope, names in [*GROUPS.items(), *((f, [f]) for f in FIELD_ORDER)]:
                ok, n = sc.accuracy(names)
                if not n:
                    continue
                row = {"setup": name, "scope": scope, "fields": n, "correct": ok,
                       "accuracy": round(ok / n, 4)}
                if scope == "all":
                    row |= {"items": sc.items, "seconds_per_item": round(sc.sec_per_item, 3),
                            "cost_per_1000_items": round(sc.cost_per_1000, 4)}
                if sc.setup in DECIDERS:
                    t = {k: sum(sc.tiers.get(f, {}).get(k, 0) for f in names) for k in TIERS}
                    asked = [ok for f in names for ok in sc.clarify.get(f, [])]
                    row |= t | {"loops": sum(sc.loops.get(f, 0) for f in names),
                                "cap_hits": sum(sc.cap_hits.get(f, 0) for f in names),
                                "clarify": len(asked), "clarify_wrong": len(asked) - sum(asked)}
                w.writerow([row.get(c, "") for c in cols])
            for f, near in sc.near.items():
                row = {"setup": name, "scope": f"{f} within one level", "fields": len(near),
                       "correct": sum(near), "accuracy": round(sum(near) / len(near), 4)}
                w.writerow([row.get(c, "") for c in cols])
            for kind, secs in sc.seconds_by_kind.items():
                w.writerow([name, f"{kind} items", len(secs), "", "", "", round(mean(secs), 3)]
                           + [""] * (len(cols) - 7))


def write_chart(scores: list[Score]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"spark": "#2a6fdb", "glimmer": "#d9822b", "fast": "#7a52c7", "system_one": "#1f9e6e",
              "joint": "#0f6b4a", "d1": "#c2417a"}
    fig, ax = plt.subplots(figsize=(8, 5.2), dpi=150)
    for sc in scores:
        ok, n = sc.accuracy(GROUPS["all"])
        if not n or not sc.sec_per_item:
            continue
        x, y = sc.sec_per_item, 100 * ok / n
        ax.scatter([x], [y], s=70, color=colors[sc.setup], zorder=3)
        ax.annotate(f"{SETUPS[sc.setup]}\n{_money(sc.cost_per_1000)} per 1,000 items", (x, y),
                    textcoords="offset points", xytext=(8, 6), fontsize=8.5, color="#222")
    ax.set_xscale("log")
    ax.set_xlabel("Seconds per item (wall clock, log scale)")
    ax.set_ylabel("Accuracy on Max's labels (all fields, %)")
    ax.set_title("System One vs Spark vs base Glimmer", fontsize=11)
    ax.grid(True, which="both", color="#e5e5e5", linewidth=0.6, zorder=0)
    lo, hi = ax.get_xlim()
    ax.set_xlim(lo, hi * 4)  # room for the labels
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi + (hi - lo) * 0.08)  # and above the top point's
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.text(0.01, 0.01, f"Glimmer: GPU seconds at ${GPU_HOUR:.2f}/h (Modal A10 proxy). "
             "Spark: API usage at Standard prices.", fontsize=7, color="#666")
    fig.tight_layout()
    fig.savefig(OUT / "compare.png")
    plt.close(fig)


def write_summary(items: list[Item], skipped: list[Skipped], scores: list[Score], seed: int,
                  llama_model: str) -> str:
    from app.config import settings as app_settings

    kinds = {}
    for i in items:
        kinds[i.kind] = kinds.get(i.kind, 0) + 1
    labeled = sum(len(i.labels) for i in items)
    by = {sc.setup: sc for sc in scores}
    lines = [
        "# System One comparison",
        "",
        f"Run {datetime.now():%b %d, %Y} on the labeled set: {kinds.get(ASSIGNMENT_KIND, 0)} assignments and "
        f"{kinds.get('test_question', 0)} test questions, {labeled} labeled fields. Glimmer is {llama_model} "
        f"on the RTX 3090 (seed {seed}, prompt version {settings.prompt_version}); Spark is "
        f"{app_settings.meta_model}. Every setup gets the same item text and the same questions and options "
        "from `decisions/fields.py`.",
        "",
        "| Setup | Accuracy | Expensive fields | Cheap fields | Seconds per item | Cost per 1,000 items |",
        "|---|---|---|---|---|---|",
    ]
    for sc in scores:
        acc = [_pct(*sc.accuracy(GROUPS[g])) for g in ("all", "expensive", "cheap")]
        lines.append(f"| {SETUPS[sc.setup]} ({sc.items} items) | {acc[0]} | {acc[1]} | {acc[2]} | "
                     f"{sc.sec_per_item:.2f} | {_money(sc.cost_per_1000)} |")
    lines += ["", "## Accuracy by field", "",
              "| Field | Cost | " + " | ".join(SETUPS[sc.setup] for sc in scores) + " |",
              "|---|---|" + "---|" * len(scores)]
    for f in FIELD_ORDER:
        cells = [_pct(*sc.accuracy([f])) for sc in scores]
        n = max(sc.accuracy([f])[1] for sc in scores) if scores else 0
        if n:
            lines.append(f"| {f} ({n}) | {COST[f]} | " + " | ".join(cells) + " |")
        if n and f in ORDINAL:
            cells = [_pct(sum(sc.near.get(f, [])), len(sc.near.get(f, []))) for sc in scores]
            lines.append(f"| {f}, within one level | {COST[f]} | " + " | ".join(cells) + " |")

    def row(name: str, t: dict, loops: int, caps: int, asked: list[bool]) -> str:
        n = sum(t.values())
        return (f"| {name} | {_pct(t['instant'], n)} | {_pct(t['thought'], n)} | {_pct(t['guessed'], n)} | "
                f"{_pct(t['default'], n)} | {loops} | {caps} ({_pct(caps, loops)}) | "
                f"{len(asked)} ({len(asked) - sum(asked)}) |")

    for setup in DECIDERS:
        one = by.get(setup)
        if not one:
            continue
        lines += ["", f"## {SETUPS[setup]}: tiers", "",
                  "| Field | Instant | Thought | Guessed | From the type | Thinking loops | Hit the cap | To clarify (wrong) |",
                  "|---|---|---|---|---|---|---|---|"]
        for f in FIELD_ORDER:
            if one.tiers.get(f):
                lines.append(row(f, one.tiers[f], one.loops.get(f, 0), one.cap_hits.get(f, 0), one.clarify.get(f, [])))
        tot = {k: sum(t[k] for t in one.tiers.values()) for k in TIERS}
        lines.append(row("all", tot, sum(one.loops.values()), sum(one.cap_hits.values()),
                         [ok for v in one.clarify.values() for ok in v]))
        mode = "joint" if setup == "joint" else "fields"
        lines += ["", f"Lines: `{lines_name(mode)}` = `{lines_json(mode)}`."]
        if setup == "joint":
            lines += ["", f"Its test questions are decided per field with `{lines_name()}`, as in "
                          f"{SETUPS['system_one']}."]
    lines += ["", "## How the numbers are made", ""]
    lines.append("- Seconds per item is wall clock per call. The decider gets a graded test's questions in "
                 "one call, as Part B will, so each question counts that call's time divided by its questions. "
                 "The fast pass alone is timed inside the same runs (the forward probe, plus the reversed "
                 "probe when the forward one clears) and answers with the forward probe's top option.")
    sp = by.get("spark")
    if sp:
        u = sp.usage
        lines.append(f"- Spark's cost is the `usage` the API returned: {u['input']:,} input tokens "
                     f"({u['cached']:,} cached) and {u['output']:,} output tokens ({u['reasoning']:,} of them reasoning, at "
                     f"Spark's default effort) over {sp.items} calls, at "
                     f"${SPARK_PRICES['input']}, ${SPARK_PRICES['cached']} and ${SPARK_PRICES['output']} per "
                     f"million (Standard mode): {_money(sp.cost)} in all.")
    lines.append(f"- Glimmer's cost prices GPU seconds at ${GPU_HOUR:.2f} an hour (Modal's A10, as a rental "
                 "proxy). On Max's own card the real cost is electricity.")
    gl = by.get("glimmer")
    if gl:
        lines.append(f"- Base Glimmer: {gl.unparsed} answers couldn't be read from its reply and count as "
                     f"wrong; {gl.truncated} replies hit the {GLIMMER_MAX_TOKENS}-token limit.")
    d1 = by.get("d1")
    if d1:
        lines.append(f"- Liquid d1-2B answers every field of an item in one `/v1/systemone` call, each a choice "
                     "question whose instructions hold the field's question, rules and worked examples, with "
                     f"each option a criterion. Its GPU seconds are priced like Glimmer's. {d1.unparsed} "
                     "answers weren't one of the options and count as wrong.")
    if any(f in ORDINAL for f in FIELD_ORDER):
        lines.append("- Within one level: effort's options run from one short sitting to multi-day, so an "
                     "answer one size off the label counts there.")
    if skipped:
        reasons: dict[str, int] = {}
        for s in skipped:
            reasons[s.reason] = reasons.get(s.reason, 0) + 1
        lines.append("- Skipped labels: " + "; ".join(f"{n} ({r})" for r, n in reasons.items()) + ".")
    text = "\n".join(lines) + "\n"
    (OUT / "summary.md").write_text(text, encoding="utf-8")
    return text


def lines_name(mode: str = "fields") -> str:
    from eval.run import lines_path

    p = lines_path(mode)
    return p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else str(p)


def lines_json(mode: str = "fields") -> str:
    from eval.run import lines_path

    p = lines_path(mode)
    if not p.exists():
        return "(none yet)"
    return json.dumps(json.loads(p.read_text(encoding="utf-8")), separators=(", ", ": "))


AUDITED = ("spark", "glimmer", "system_one")


def write_audit(items: list[Item], recs: dict[str, dict[str, dict]]) -> int:
    """data/eval/audit.md: every label that Spark, base Glimmer and System One all answer otherwise,
    with each setup's answer, to check the label by hand. Real item text, so it stays under data/.
    Returns how many."""
    rows = []
    for item in items:
        got = {s: recs[s].get(item.key) for s in AUDITED if recs.get(s)}
        if len(got) < len(AUDITED) or any(r is None for r in got.values()):
            continue
        for f, label in item.labels.items():
            answers = {s: r["answers"].get(f) for s, r in got.items()}
            if all(a != label for a in answers.values()):
                rows.append((item, f, label, answers))
    out = ["# Labels every setup misses", "",
           f"{len(rows)} labels that {', '.join(SETUPS[s] for s in AUDITED)} all answer otherwise. When the "
           "three agree with each other, check the label first.", ""]
    for item, f, label, answers in rows:
        same = len(set(answers.values())) == 1
        title = item.context().splitlines()[0][:100]
        out.append(f"- `{item.key}` {f}: labeled **{label}**; "
                   + ", ".join(f"{SETUPS[s]} {a}" for s, a in answers.items())
                   + (" (all three agree)" if same else "") + f". {title}")
    EVAL_DATA.mkdir(parents=True, exist_ok=True)
    (EVAL_DATA / "audit.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(rows)


def write_outputs(items: list[Item], skipped: list[Skipped], seed: int) -> None:
    recs = {s: load_records(s) for s in RUNNABLE}
    scores = []
    for setup in SETUPS:
        got = recs["system_one"] if setup == "fast" else recs[setup]
        if got:
            scores.append(score(setup, items, got))
    if not scores:
        raise SystemExit("No compare records yet: run compare without --report first.")
    model = next((r["run"]["model"] for s in ("system_one", "glimmer") for r in recs[s].values()), "Glimmer")
    OUT.mkdir(exist_ok=True)
    write_csv(scores)
    write_chart(scores)
    print(write_summary(items, skipped, scores, seed, model))
    audited = write_audit(items, recs)
    print(f"Wrote eval/results.csv, eval/summary.md and eval/compare.png; data/eval/audit.md lists {audited} "
          "labels every setup misses.")
