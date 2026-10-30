"""Keep-or-cut check for the dry run. CLAUDE.md: cut it unless its traps match where the student
struggled on three finished assignments.

The dry run never sees grades or feedback, so this is a fair test. For each finished assignment
with a saved run, a trap counts as a match when its rubric_criterion names a criterion the student
lost points on. Teacher comments aren't tied to a criterion, so they're printed beside the traps to
judge by eye.

  python -m stretch.evaluate              # finished assignments with feedback to test on
  python -m stretch.evaluate 12 31 40     # compare their saved runs with the feedback
"""

import argparse
import sqlite3
import sys

from app import db
from app.models import Assignment
from stretch import dry_run

NEEDED = 3


def struggles(a: Assignment) -> tuple[list[dict], list[str]]:
    """(rubric criteria where points were lost, teacher comments) for one graded assignment."""
    lost = [
        {"criterion": r.criterion, "lost": r.points_possible - r.points, "possible": r.points_possible, "comment": r.comment}
        for r in a.rubric
        if r.points is not None and r.points_possible and r.points < r.points_possible
    ]
    return lost, [c.text for c in a.comments if c.text.strip()]


def candidates(conn: sqlite3.Connection) -> list[Assignment]:
    """Graded assignments with lost rubric points or teacher comments, most recently graded first."""
    graded = [a for a in db.list_assignments(conn) if a.graded_at or a.score is not None]
    out = [a for a in graded if any(struggles(a))]
    return sorted(out, key=lambda a: a.graded_at.isoformat() if a.graded_at else "", reverse=True)


def compare(a: Assignment, result: dry_run.DryRun) -> dict:
    lost, comments = struggles(a)
    flagged = {dry_run.norm(t.rubric_criterion) for t in result.traps if t.rubric_criterion}
    matched = [l["criterion"] for l in lost if dry_run.norm(l["criterion"]) in flagged]
    return {
        "matched": matched,
        "missed": [l for l in lost if l["criterion"] not in matched],
        "comments": comments,
        "traps": result.traps,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m stretch.evaluate", description=__doc__.splitlines()[0])
    parser.add_argument("assignment_ids", type=int, nargs="*")
    args = parser.parse_args(argv)
    conn = db.connect()
    db.init_db(conn)
    try:
        if not args.assignment_ids:
            found = candidates(conn)
            if not found:
                sys.exit("No graded assignments with lost rubric points or teacher comments yet.")
            print("Finished assignments with feedback to test on (run each with python -m stretch.dry_run run <id>):")
            for a in found:
                lost, comments = struggles(a)
                print(f"  {a.id}  {a.name}  ({len(lost)} lost criteria, {len(comments)} comments)")
            return

        hits = 0
        for assignment_id in args.assignment_ids:
            a = db.get_assignment(conn, assignment_id)
            result = dry_run.latest(conn, assignment_id)
            if a is None or result is None:
                print(f"\n{assignment_id}: no {'assignment' if a is None else 'dry run'} yet\n")
                continue
            c = compare(a, result)
            hits += bool(c["matched"])
            print(f"\n== {a.name} ({result.model}) ==")
            print(f"Criterion matches: {', '.join(c['matched']) or 'none'}")
            for l in c["missed"]:
                note = f": \"{l['comment']}\"" if l["comment"] else ""
                print(f"  not flagged: {l['criterion']} (lost {l['lost']:g}/{l['possible']:g}){note}")
            if c["comments"]:
                print("Teacher comments (judge by eye):")
                for text in c["comments"]:
                    print(f"  \"{text}\"")
            print("Traps:")
            for t in c["traps"]:
                print(f"  - [{t.kind}] {t.where}: {t.what_happens}" + (f" (rubric: {t.rubric_criterion})" if t.rubric_criterion else ""))
        print(
            f"\n{hits} of {len(args.assignment_ids)} matched by rubric criterion; {NEEDED} needed. "
            "Count comment matches you judge real on top of that."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
