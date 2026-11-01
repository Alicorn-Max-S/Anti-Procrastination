"""Pull last school year's assignments from Canvas (read-only) and draw the labeling sample.

    python -m labeling.fetch_last_year [--n 100] [--seed 31] [--force]

Finds the completed courses whose term started in the 2025/26 school year, drops the non-academic
ones, fetches every assignment with the app's own Canvas client, and picks a random sample.
Writes data/labeling/last_year.json (gitignored: this repo is public) and never touches app.db.
Labels are keyed by assignment id, so a second run refuses to redraw the sample without --force.
"""

import argparse
import json
import random
import sys
from datetime import datetime, timezone

import httpx

from app import canvas
from labeling.courses import is_academic
from labeling.server import DATA, LAST_YEAR

TERM_STARTS_FROM, TERM_STARTS_BEFORE = "2025-08-01", "2026-08-01"


def last_year_courses() -> list[dict]:
    with canvas._client() as c:
        raw = canvas._get_all(c, "/api/v1/courses", [("enrollment_state", "completed"), ("include[]", "term")])
    out = []
    for r in raw:
        term = r.get("term") or {}
        start = (term.get("start_at") or "")[:10]
        if r.get("name") and TERM_STARTS_FROM <= start < TERM_STARTS_BEFORE:
            season = (term.get("name") or "").rsplit(" - ", 1)[-1].split()[0]
            out.append({"id": r["id"], "name": r["name"], "label": f"{r['name']} — {season} {start[:4]}"})
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=100)
    p.add_argument("--seed", type=int, default=31)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    if LAST_YEAR.exists() and not args.force:
        sys.exit(f"{LAST_YEAR} already exists. Labels are keyed by assignment id, so redrawing the sample "
                 "orphans labels already made. Pass --force if you really want a new draw.")

    courses = last_year_courses()
    kept = [c for c in courses if is_academic(c["name"])]
    print("skipping as non-academic:", [c["label"] for c in courses if c not in kept] or "none")
    pool, failed = {}, []
    for c in kept:
        try:
            found = canvas.fetch_assignments(c["id"])
        except httpx.HTTPError as e:
            failed.append((c["label"], str(e)))
            continue
        pool.update({a.id: a for a in found})
        print(f"  {c['label']}: {len(found)} assignments")
    for label, err in failed:
        print("  FAILED", label, err)

    sample = random.Random(args.seed).sample(sorted(pool), min(args.n, len(pool)))
    DATA.mkdir(parents=True, exist_ok=True)
    LAST_YEAR.write_text(json.dumps({
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": args.seed,
        "pool_size": len(pool),
        "courses": {str(c["id"]): c["label"] for c in kept},
        "sample_ids": sample,
        "assignments": {str(i): pool[i].model_dump(mode="json") for i in pool},
    }))
    print(f"pool of {len(pool)} assignments from {len(kept) - len(failed)} courses; sampled {len(sample)}")


if __name__ == "__main__":
    main()
