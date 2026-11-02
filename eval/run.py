"""System One eval harness (spec, Part C). From the repo root, with llama-server running:

    python -m eval.run calibrate [--mode fields|joint] [--report] [--approve] [--slots N] [--limit N] [--seed 7] [--fresh]
    python -m eval.run compare [--setups spark glimmer system_one joint d1] [--report] [--limit N] [--seed 7] [--fresh]

calibrate records every labeled field (resuming where it stopped), prints the per-field report and
the proposed lines, and asks before writing decisions/lines.json. Without a terminal to ask in it
stops there, and `calibrate --approve` writes the same proposal from the records with no model
calls. --mode joint does the same for joint mode, into decisions/lines.joint.json. compare needs
those lines files. It runs the setups (resuming too), then writes eval/results.csv,
eval/summary.md and eval/compare.png. --report on either one only reports.

The d1 setup needs Liquid's d1-2B on its own llama-server (D1_URL, default port 8081):
    llama-server -m models/d1-2B-Q8_0.gguf -b 8192 -ub 8192 --port 8081 -ngl 99
"""

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script (python eval/run.py): import from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decider.config import MODES, ROOT, settings  # noqa: E402
from decider.llama import Llama  # noqa: E402
from eval import calibrate, compare  # noqa: E402
from eval.items import EVAL_DATA, LABELS, load  # noqa: E402


def lines_path(mode: str = "fields") -> Path:
    p = Path(settings.joint_lines_file if mode == "joint" else settings.lines_file)
    return p if p.is_absolute() else ROOT / p


def _shown(path: Path) -> str:
    return str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)


def write_lines(prop: dict, mode: str = "fields") -> None:
    path = lines_path(mode)
    path.write_text(json.dumps(prop, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {_shown(path)}.")


def llama_up() -> Llama:
    llama = Llama(settings.glimmer_url)
    if not llama.healthy():
        raise SystemExit(f"llama-server isn't answering at {settings.glimmer_url} (spec, Serving).")
    return llama


def run_calibrate(args: argparse.Namespace) -> None:
    items, skipped = load(args.labels)
    items = items[:args.limit] if args.limit else items
    if not (args.report or args.approve):
        calibrate.record_all(llama_up(), settings, items, args.seed, args.fresh, args.slots, args.mode)
    text, prop, _ = calibrate.report(items, skipped, settings.letter_share_min,
                                     settings.template_from_type, args.mode)
    print(text)
    suffix = "-joint" if args.mode == "joint" else ""
    EVAL_DATA.mkdir(parents=True, exist_ok=True)
    (EVAL_DATA / f"calibrate-report{suffix}.txt").write_text(text + "\n", encoding="utf-8")
    (EVAL_DATA / f"proposal{suffix}.json").write_text(json.dumps(prop, indent=2) + "\n", encoding="utf-8")
    target = _shown(lines_path(args.mode))
    if args.approve:
        write_lines(prop, args.mode)
    else:
        answer = ""
        if sys.stdin.isatty():
            try:
                answer = input(f"\nWrite this proposal to {target}? [y/N] ").strip().lower()
            except EOFError:
                pass
        if answer in ("y", "yes"):
            write_lines(prop, args.mode)
        else:
            flag = " --mode joint" if args.mode == "joint" else ""
            print(f"\nNot written: approve it with `python -m eval.run calibrate{flag} --approve`.")


def run_compare(args: argparse.Namespace) -> None:
    items, skipped = load(args.labels)
    items = items[:args.limit] if args.limit else items
    if not args.report:
        if {"system_one", "joint"} & set(args.setups) and not lines_path().exists():
            raise SystemExit("No decisions/lines.json yet: approve the calibrate proposal first.")
        if "joint" in args.setups and not lines_path("joint").exists():
            raise SystemExit(f"No {_shown(lines_path('joint'))} yet: run `calibrate --mode joint` and approve it.")
        if "d1" in args.setups and not Llama(compare.D1_URL).healthy():
            raise SystemExit(f"d1-2B's llama-server isn't answering at {compare.D1_URL} (D1_URL).")
        llama = llama_up() if {"glimmer", "system_one", "joint"} & set(args.setups) else None
        compare.run_setups(llama, items, args.setups, args.seed, args.fresh)
    compare.write_outputs(items, skipped, args.seed)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="System One eval harness (spec, Part C).")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("calibrate", "compare"):
        p = sub.add_parser(name)
        p.add_argument("--labels", type=Path, default=LABELS, help="labels.jsonl to score against")
        p.add_argument("--seed", type=int, default=7, help="sampling seed for every thinking call")
        p.add_argument("--limit", type=int, help="only the first N items (a smoke run)")
        p.add_argument("--fresh", action="store_true", help="redo every record instead of resuming")
        p.add_argument("--report", action="store_true", help="no model calls: report from the records")
    sub.choices["calibrate"].add_argument("--approve", action="store_true",
                                          help="write the proposal to decisions/lines.json (no model calls)")
    sub.choices["calibrate"].add_argument("--slots", type=int, default=settings.parallel_slots,
                                          help="items recorded at once; llama-server needs -np at least this")
    sub.choices["calibrate"].add_argument("--mode", choices=MODES, default="fields",
                                          help="joint: every assignment field in one prompt and one chain, "
                                               "to decisions/lines.joint.json")
    sub.choices["compare"].add_argument("--setups", nargs="+", choices=compare.RUNNABLE,
                                        default=list(compare.RUNNABLE),
                                        help="setups to run (fast pass only comes from system_one)")
    args = ap.parse_args()
    {"calibrate": run_calibrate, "compare": run_compare}[args.command](args)


if __name__ == "__main__":
    main()
