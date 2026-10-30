"""One-shot sync: Canvas -> SQLite. Run with `python -m app.sync`.

Glues canvas.py to db.py: fetch every course's assignments, tag the kind, fetch feedback for
newly graded work, save. The background loop (loop.py) calls this every few minutes.

Canvas requests run in parallel on one shared client (CANVAS_WORKERS at a time); DB writes stay
on the caller's thread and connection, and rows that didn't change aren't rewritten.
"""

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx

from app import canvas, db
from app.models import Assignment
from app.progress import Progress

log = logging.getLogger(__name__)

CANVAS_WORKERS = 8


def _needs_feedback(new: Assignment, old: Assignment | None) -> bool:
    """Feedback costs one request per assignment, so only refetch when the grade changed."""
    if old is None:
        return new.submitted or new.graded_at is not None
    return new.graded_at != old.graded_at


def _carry_over(new: Assignment, old: Assignment) -> Assignment:
    """Keep the comments and rubric scores fetched on an earlier sync."""
    scored = any(r.points is not None for r in old.rubric)
    return new.model_copy(update={"comments": old.comments, "rubric": old.rubric if scored else new.rubric})


def sync_all(conn: sqlite3.Connection, progress: Progress | None = None) -> int:
    """Pull everything from Canvas into the DB. Returns the number of assignments saved.

    A course whose assignments can't be fetched is reported in `progress` and skipped; its rows
    from the last sync stay as they were."""
    progress = progress or Progress()
    old = {a.id: a for a in db.list_assignments(conn)}
    with canvas._client() as client, ThreadPoolExecutor(max_workers=CANVAS_WORKERS) as pool:
        progress.phase("courses")
        courses = canvas.fetch_courses(client)
        for course in courses:
            db.save_course(conn, course)

        progress.phase("assignments", total=len(courses))

        def assignments(course) -> list[Assignment]:
            progress.begin(course.name)
            try:
                return canvas.fetch_assignments(course.id, client)
            except httpx.HTTPError as e:
                log.warning("assignments for %s failed: %s", course.name, e)
                progress.fail(course.name, f"{type(e).__name__}: {e}")
                return []
            finally:
                progress.end(course.name)

        fetched = [a for batch in pool.map(assignments, courses) for a in batch]
        need = [a for a in fetched if _needs_feedback(a, old.get(a.id))]
        progress.phase("feedback", total=len(need))

        def feedback(a: Assignment) -> Assignment:
            progress.begin(a.name)
            try:
                return canvas.fetch_feedback(a.course_id, a, client)
            except httpx.HTTPError as e:
                log.warning("feedback for %s failed: %s", a.id, e)
                return _carry_over(a, old[a.id]) if a.id in old else a
            finally:
                progress.end(a.name)

        with_feedback = {a.id: a for a in pool.map(feedback, need)}

    for a in fetched:
        if a.id in with_feedback:
            a = with_feedback[a.id]
        elif a.id in old:
            a = _carry_over(a, old[a.id])
        if old.get(a.id) != a:
            db.save_assignment(conn, a)
    return len(fetched)


if __name__ == "__main__":
    from app.db import connect, init_db

    conn = connect()
    init_db(conn)
    print(f"Synced {sync_all(conn)} assignments")
