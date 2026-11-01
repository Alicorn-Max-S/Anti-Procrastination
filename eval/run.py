"""System One eval harness (spec, Part C). From the repo root, with llama-server running:

    python -m eval.run calibrate [--report] [--approve] [--slots N] [--limit N] [--seed 7] [--fresh]
    python -m eval.run compare [--setups spark glimmer system_one] [--report] [--limit N] [--seed 7] [--fresh]

calibrate records every labeled field (resuming where it stopped), prints the per-field report and
the proposed lines, and asks before writing decisions/lines.json. Without a terminal to ask in it
stops there, and `calibrate --approve` writes the same proposal from the records with no model
calls. compare needs that lines file. It runs the setups (resuming too), then writes
eval/results.csv, eval/summary.md and eval/compare.png. --report on either one only reports.
"""

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script (python eval/run.py): import from the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decider.config import ROOT, settings  # noqa: E402
from decider.llama import Llama  # noqa: E402
from eval import calibrate, compare  # noqa: E402
from eval.items import EVAL_DATA, LABELS, load  # noqa: E402


def lines_path() -> Path:
    p = Path(settings.lines_file)
    return p if p.is_absolute() else ROOT / p


def write_lines(prop: dict) -> None:
    path = lines_path()
    path.write_text(json.dumps(prop, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}.")


def llama_up() -> Llama:
    llama = Llama(settings.glimmer_url)
    if not llama.healthy():
        raise SystemExit(f"llama-server isn't answering at {settings.glimmer_url} (spec, Serving).")
    return llama


def run_calibrate(args: argparse.Namespace) -> None:
    items, skipped = load(args.labels)
    items = items[:args.limit] if args.limit else items
    if not (args.report or args.approve):
        calibrate.record_all(llama_up(), settings, items, args.seed, args.fresh, args.slots)
    text, prop, _ = calibrate.report(items, skipped, settings.letter_share_min)
    print(text)
    EVAL_DATA.mkdir(parents=True, exist_ok=True)
    (EVAL_DATA / "calibrate-report.txt").write_text(text + "\n", encoding="utf-8")
    (EVAL_DATA / "proposal.json").write_text(json.dumps(prop, indent=2) + "\n", encoding="utf-8")
    if args.approve:
        write_lines(prop)
    else:
        answer = ""
        if sys.stdin.isatty():
            try:
                answer = input("\nWrite this proposal to decisions/lines.json? [y/N] ").strip().lower()
            except EOFError:
                pass
        if answer in ("y", "yes"):
            write_lines(prop)
        else:
            print("\nNot written: approve it with `python -m eval.run calibrate --approve`.")


def run_compare(args: argparse.Namespace) -> None:
    items, skipped = load(args.labels)
    items = items[:args.limit] if args.limit else items
    if not args.report:
        if "system_one" in args.setups and not lines_path().exists():
            raise SystemExit("No decisions/lines.json yet: approve the calibrate proposal first.")
        llama = llama_up() if {"glimmer", "system_one"} & set(args.setups) else None
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
    sub.choices["compare"].add_argument("--setups", nargs="+", choices=compare.RUNNABLE,
                                        default=list(compare.RUNNABLE),
                                        help="setups to run (fast pass only comes from system_one)")
    args = ap.parse_args()
    {"calibrate": run_calibrate, "compare": run_compare}[args.command](args)


if __name__ == "__main__":
    main()
