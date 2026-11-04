"""race: Spark and Glimmer label the same real graded test at the same time, for the demo.

    python -m eval.race [--test test-002] [--limit N]

Both lanes take the test's questions in order, one at a time, and label each one's format, topic and
credit. Spark gets one structured-output call per question (eval.compare.spark_item, the prompt the
comparison used). Glimmer goes through the decider the way the app does it: thinking off, the fast
pass alone (app.decider_client.decide_test). The lanes run together on one clock, so every question's
start and finish is real, and both are scored against Max's labels.

Writes data/demo/race.json: per question and lane, when it started and finished (seconds from the
start of the race), the labels given and whether each matched, plus a summary per lane. No question
text and no student answers. `python -m eval.race_media` turns it into the diagram and the video.
Needs the decider (`python -m decider`) and llama-server running, and META_API_KEY set. It spends
Spark tokens: about a cent a question.
"""

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, median

if __package__ in (None, ""):  # run as a script (python eval/race.py): import from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import decider_client, meta_client  # noqa: E402
from app.config import settings as app_settings  # noqa: E402
from eval.compare import GPU_HOUR, groups, spark_item  # noqa: E402
from eval.items import ROOT, Item, load  # noqa: E402

OUT = ROOT / "data" / "demo"
RACE = OUT / "race.json"
FIELDS = ("format", "topic", "credit")


def pick_test(items: list[Item], test: str | None) -> list[Item]:
    """The named test's labeled questions, or the biggest test there is."""
    tests = [g for g in groups(items) if g[0].kind == "test_question"]
    if test:
        tests = [g for g in tests if g[0].test == test]
    if not tests:
        raise SystemExit(f"No labeled test{f' called {test}' if test else ''}: see `python -m eval.run compare`.")
    return max(tests, key=len)


def _scored(item: Item, answers: dict[str, str | None]) -> dict[str, bool]:
    return {f: answers.get(f) == label for f, label in item.labels.items()}


def _lane(name: str, items: list[Item], t0: float, work, out: list, errors: dict) -> None:
    """One lane: `work(item)` labels a question; its start and finish are timed against `t0`."""
    try:
        for n, item in enumerate(items, 1):
            start = time.perf_counter() - t0
            got = work(item)
            end = time.perf_counter() - t0
            answers = got.pop("answers")
            right = _scored(item, answers)
            out.append({
                "n": n, "key": item.key, "number": str(item.data.get("number", n)),
                "start": round(start, 3), "end": round(end, 3), "seconds": round(end - start, 3),
                "answers": answers, "labels": item.labels, "right": right, **got,
            })
            print(f"  {name:<8} [{n}/{len(items)}] {end - start:5.1f} s  "
                  f"{sum(right.values())}/{len(right)} right", flush=True)
    except Exception as e:  # one lane failing mustn't stop the other
        errors[name] = f"{type(e).__name__}: {e}"
        print(f"  {name}: stopped, {errors[name]}", flush=True)


def summarize(name: str, rows: list[dict]) -> dict:
    marks = [ok for r in rows for ok in r["right"].values()]
    by_field = {f: [r["right"][f] for r in rows if f in r["right"]] for f in FIELDS}
    secs = [r["seconds"] for r in rows]
    out = {
        "questions": len(rows),
        "wall_seconds": round(max((r["end"] for r in rows), default=0.0), 2),
        "mean_seconds": round(mean(secs), 2) if secs else 0.0,
        "median_seconds": round(median(secs), 2) if secs else 0.0,
        "labeled_fields": len(marks),
        "right": sum(marks),
        "accuracy": round(sum(marks) / len(marks), 4) if marks else 0.0,
        "by_field": {f: {"right": sum(v), "of": len(v)} for f, v in by_field.items() if v},
    }
    if name == "spark":
        out["cost"] = round(sum(r.get("cost", 0.0) for r in rows), 4)
        out["input_tokens"] = sum(r.get("usage", {}).get("input_tokens", 0) for r in rows)
        out["output_tokens"] = sum(r.get("usage", {}).get("output_tokens", 0) for r in rows)
    else:  # Glimmer on Max's own card: its GPU seconds at the comparison's rental proxy
        out["cost"] = round(out["wall_seconds"] / 3600 * GPU_HOUR, 4)
        out["tiers"] = {t: sum(r.get("tiers", {}).get(f) == t for r in rows for f in FIELDS)
                        for t in ("instant", "thought", "guessed", "default")}
    return out


def run(test: str | None, limit: int | None) -> dict:
    if not meta_client.configured():
        raise SystemExit("META_API_KEY isn't set: Spark can't run.")
    if not decider_client.healthy():
        raise SystemExit(f"The decider isn't answering at {app_settings.decider_url} (python -m decider).")
    items, _ = load()
    group = pick_test(items, test)
    items = group[:limit] if limit else group
    print(f"race: {len(items)} questions of {items[0].test} ({items[0].course}), "
          f"{sum(len(i.labels) for i in items)} labeled fields", flush=True)

    client = meta_client._client()

    def spark(item: Item) -> dict:
        r = spark_item(client, item)
        return {"answers": r["answers"], "usage": r["usage"], "cost": r["cost"], "retries": r["retries"]}

    def glimmer(item: Item) -> dict:
        question = {k: v for k, v in item.data.items() if k != "id"}
        decided = decider_client.decide_test(0, item.course, item.topics, [question])
        if not decided:
            raise RuntimeError("the decider didn't answer")
        d = decided[0]
        return {"answers": {f: d[f]["value"] for f in FIELDS if f in d},
                "tiers": {f: d[f]["tier"] for f in FIELDS if f in d},
                "confidence": {f: d[f]["confidence"] for f in FIELDS if f in d}}

    lanes: dict[str, list] = {"spark": [], "glimmer": []}
    errors: dict[str, str] = {}
    t0 = time.perf_counter()
    threads = [threading.Thread(target=_lane, args=(name, items, t0, work, lanes[name], errors))
               for name, work in (("spark", spark), ("glimmer", glimmer))]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        client.close()

    result = {
        "when": datetime.now().isoformat(timespec="seconds"),
        "test": items[0].test, "course": items[0].course, "questions": len(items),
        "spark_model": app_settings.meta_model, "glimmer_model": "Muse-Glimmer-30B Q4_K_M, RTX 3090",
        "errors": errors,
        "lanes": lanes,
        "summary": {name: summarize(name, rows) for name, rows in lanes.items() if rows},
    }
    OUT.mkdir(parents=True, exist_ok=True)
    RACE.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    for name, s in result["summary"].items():
        print(f"{name}: {s['questions']} questions in {s['wall_seconds']} s ({s['mean_seconds']} s each), "
              f"{s['right']}/{s['labeled_fields']} labels right ({s['accuracy']:.1%}), ${s['cost']}")
    print(f"Wrote {RACE.relative_to(ROOT)}")
    return result


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Spark and Glimmer label one real test side by side.")
    ap.add_argument("--test", help="a labeled test id (default: the biggest)")
    ap.add_argument("--limit", type=int, help="only the first N questions (a smoke run)")
    args = ap.parse_args()
    run(args.test, args.limit)


if __name__ == "__main__":
    main()
