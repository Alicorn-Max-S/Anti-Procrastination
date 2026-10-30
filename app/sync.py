"""One-shot sync: Canvas -> SQLite. Run with `python -m app.sync`.

Glues canvas.py to db.py: for each course, fetch assignments, tag the kind, fetch feedback for
newly graded work, save. The background loop (loop.py) calls this every few minutes.
"""

import logging
import sqlite3

import httpx

from app import canvas, db
from app.models import Assignment

log = logging.getLogger(__name__)


def _needs_feedback(new: Assignment, old: Assignment | None) -> bool:
    """Feedback costs one request per assignment, so only refetch when the grade changed."""
    if old is None:
        return new.submitted or new.graded_at is not None
    return new.graded_at != old.graded_at


def sync_all(conn: sqlite3.Connection) -> int:
    """Pull everything from Canvas into the DB. Returns the number of assignments saved."""
    count = 0
    for course in canvas.fetch_courses():
        db.save_course(conn, course)
        for a in canvas.fetch_assignments(course.id):
            old = db.get_assignment(conn, a.id)
            if _needs_feedback(a, old):
                try:
                    a = canvas.fetch_feedback(course.id, a)
                except httpx.HTTPError as e:
                    log.warning("feedback for %s failed: %s", a.id, e)
            elif old is not None:
                scored = any(r.points is not None for r in old.rubric)
                a = a.model_copy(
                    update={"comments": old.comments, "rubric": old.rubric if scored else a.rubric}
                )
            db.save_assignment(conn, a)
            count += 1
    return count


if __name__ == "__main__":
    from app.db import connect, init_db

    conn = connect()
    init_db(conn)
    print(f"Synced {sync_all(conn)} assignments")
