"""Live check against a real llama-server: decide 3 labelled assignments and 1 graded test through
the API, print every field's value, tier, confidence and trace length, then check the cache.

    python -m decider.live_check --test-db PATH [--server-log PATH] [--questions N]

The cache check walks every /completion call: the tokens the slot held after the previous call vs
this prompt give the tail it should process, and llama-server's `prompt_n` says what it did
process. With --server-log, it also counts "forcing full prompt re-processing" lines written during
the run. Real data stays on this machine: it only reads the app DB, labels.jsonl and the test DB.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from . import main
from .config import settings
from .decide import Decider
from .llama import Llama

CACHE_SLACK = 2  # the last generated token is not in the cache yet, so it gets processed again
FULL_REPROCESS = "forcing full prompt re-processing"


def lcp(a: list, b: list) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def pick_assignments(n: int, ids: list[str] | None) -> list[tuple[dict, dict]]:
    """(item, hand labels): one each of the most common labelled types, unless ids are given."""
    from labeling.server import _app_db, build_items, current_labels, read_rows

    conn = _app_db()
    try:
        items, _ = build_items(conn)
    finally:
        conn.close()
    labels = current_labels(read_rows())
    labelled = [(i, labels[i["key"]]) for i in items if len(labels.get(i["key"], {})) == 3]
    if ids:
        return [p for p in labelled if p[0]["id"] in ids]
    picked, seen = [], set()
    for want in ("reading", "problem_set_or_worksheet", "test_or_quiz", "essay_or_written_response"):
        for item, lab in labelled:
            if lab["type"] == want and item["description"] and want not in seen:
                picked.append((item, lab))
                seen.add(want)
                break
    return picked[:n]


def load_test(db: Path, limit: int | None) -> tuple[dict, list[dict]]:
    """The graded test with the most rows: (request body, the grader's rows for comparison)."""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    source, course_id = conn.execute(
        "SELECT source, course_id FROM test_rows GROUP BY source ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
    course = conn.execute("SELECT name FROM courses WHERE id = ?", (course_id,)).fetchone()
    rows = conn.execute("SELECT * FROM test_rows WHERE source = ? ORDER BY id", (source,)).fetchall()
    conn.close()
    topics = list(dict.fromkeys(r["topic"] for r in rows if r["topic"]))
    rows = rows[:limit] if limit else rows

    def marks(r) -> str | None:
        if r["marks"] is None:
            return None
        return f"{r['marks']:.3g}/{r['marks_possible']:g}" if r["marks_possible"] else f"{r['marks']:.3g}"

    questions = [{
        "id": r["id"], "text": r["question"] or "", "answer": r["student_answer"] or "",
        "marks": marks(r), "note": r["teacher_note"] or "", "points": r["marks_possible"],
        "section": r["section"] or "",
    } for r in rows]
    body = {"class_id": course_id, "course": course[0] if course else "", "topics": topics,
            "questions": questions}
    return body, [dict(r) for r in rows]


def show(field: str, d: dict, hand: str = "") -> None:
    hand = f"  (hand: {hand})" if hand else ""
    print(f"    {field:<9}{d['value']:<28}{d['tier']:<9}conf {d['confidence']:.3f}  "
          f"share {d['letter_share']:.3f}  trace {len(d['trace'])}  think {d['think_tokens']:>3}  "
          f"probe_tokens {d['probe_tokens']:>4}  flipped {d['flipped']}{hand}")


def cache_report(log: list[dict], calls: list[tuple[str, int, int]]) -> bool:
    """Per decide call: every probe after the first should process only its tail."""
    held: dict[int, list[int]] = {}
    tails = []
    for r in log:
        before = held.get(r["slot"], [])
        r["tail"] = len(r["prompt"]) - lcp(before, r["prompt"])
        held[r["slot"]] = r["prompt"] + r["tokens"]
    ok = True
    print("\nCache: prompt tokens processed vs the tail beyond what the slot already held")
    print(f"  {'call':<28}{'probes':>7}{'first probe':>13}{'later probes: tail':>20}"
          f"{'processed':>11}{'worst extra':>13}{'think gens: max processed':>27}")
    for label, lo, hi in calls:
        rs = log[lo:hi]
        probes = [r for r in rs if r["kind"] == "probe"]
        later = probes[1:]
        extra = max((r["prompt_n"] - r["tail"] for r in later), default=0)
        gens = [r["prompt_n"] for r in rs if r["kind"] == "think"]
        ok &= extra <= CACHE_SLACK
        tails.extend(r["prompt_n"] for r in later)
        print(f"  {label[:27]:<28}{len(probes):>7}{probes[0]['prompt_n'] if probes else 0:>13}"
              f"{sum(r['tail'] for r in later):>20}{sum(r['prompt_n'] for r in later):>11}"
              f"{extra:>13}{max(gens, default=0):>27}")
    if tails:
        print(f"  later probes processed {min(tails)}-{max(tails)} tokens each "
              f"(median {sorted(tails)[len(tails) // 2]})")
    return ok


def main_() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Decide real items against the live llama-server.")
    ap.add_argument("--test-db", type=Path, required=True, help="a DB with grader test_rows")
    ap.add_argument("--server-log", type=Path, help="llama-server's log file, to grep")
    ap.add_argument("--questions", type=int, help="only the first N questions of the test")
    ap.add_argument("--assignments", nargs="*", help="assignment ids (default: one per type)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--skip-test", action="store_true")
    args = ap.parse_args()

    llama = Llama(settings.glimmer_url)
    llama.log = []
    main._decider = Decider(llama, settings)
    client = TestClient(main.app)
    log_start = args.server_log.stat().st_size if args.server_log else 0

    print("health:", client.get("/health").json())
    print(f"settings: cap {settings.cap_tokens}, paragraph {settings.paragraph_max}, "
          f"min chunk {settings.min_probe_chunk}, fast pass {settings.fast_pass_format}, "
          f"slots {settings.parallel_slots}, seed {args.seed}")
    calls = []

    for item, hand in pick_assignments(3, args.assignments):
        lo = len(llama.log)
        start = time.perf_counter()
        r = client.post("/decide/assignment", json={"assignment": item, "seed": args.seed})
        r.raise_for_status()
        out = r.json()
        print(f"\nassignment {item['id']}: {item['course']} / {item['title'][:60]!r} "
              f"({time.perf_counter() - start:.1f} s, {out['timings']})")
        for field, d in out["decisions"].items():
            show(field, d, hand.get(field, ""))
        calls.append((f"assignment {item['id']}", lo, len(llama.log)))

    if not args.skip_test:
        body, rows = load_test(args.test_db, args.questions)
        body["seed"] = args.seed
        lo = len(llama.log)
        start = time.perf_counter()
        r = client.post("/decide/test", json=body, timeout=3600)
        r.raise_for_status()
        out = r.json()
        wall = time.perf_counter() - start
        print(f"\ntest: {body['course']}, {len(body['questions'])} questions, topics {body['topics']} "
              f"({wall:.1f} s, {out['timings']})")
        print(f"  new topics: {out['new_topics']}")
        tiers: dict[str, dict[str, int]] = {}
        for row in rows:
            ds = out["decisions"][str(row["id"])]
            print(f"  q{row['id']} {row['question'][:50]!r} answer {row['student_answer'][:20]!r} "
                  f"marks {row['marks']}/{row['marks_possible']}")
            for field, d in ds.items():
                hand = {"topic": row["topic"], "format": f"grader qtype {row['qtype']}"}.get(field, "")
                show(field, d, hand)
                tiers.setdefault(field, {}).setdefault(d["tier"], 0)
                tiers[field][d["tier"]] += 1
        print(f"  tiers by field: {tiers}")
        calls.append((f"test ({len(body['questions'])} questions)", lo, len(llama.log)))

        # Topic naming, live: the double-pronoun questions with that topic left off the list.
        missing = [t for t in body["topics"] if t != "double_object_pronouns"]
        qs = [q for q, row in zip(body["questions"], rows) if row["topic"] == "double_object_pronouns"][:2]
        if qs and len(missing) < len(body["topics"]):
            lo = len(llama.log)
            r = client.post("/decide/test", json={**body, "topics": missing, "questions": qs,
                                                  "fields": ["topic"]})
            r.raise_for_status()
            out = r.json()
            print(f"\nnaming check: topics {missing}, {len(qs)} double-pronoun questions -> "
                  f"new_topics {out['new_topics']}")
            for qid, ds in out["decisions"].items():
                show("topic", ds["topic"])
            calls.append(("naming check", lo, len(llama.log)))

    ok = cache_report(llama.log, calls)
    if args.server_log:
        text = args.server_log.read_bytes()[log_start:].decode("utf-8", errors="replace")
        n = text.count(FULL_REPROCESS)
        print(f"\nllama-server log: {n} \"{FULL_REPROCESS}\" lines during this run")
        ok &= n == 0
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main_())
