"""Each class's subject color: one of the prep doc design's 8, stored per course.

A course without one gets the next palette color not already in use (in course-list order), so a
fresh setup is color-coded from the start. The page derives every other color from this one
(static/theme.js).
"""

import sqlite3

from app import db

# The design's fixed set, in its order (static/theme.js has the same list). Blue is the default.
SUBJECT_COLORS: tuple[str, ...] = (
    "#3D5AFE",  # blue
    "#7C3AED",  # purple
    "#D6409F",  # pink
    "#E5484D",  # red
    "#F0812A",  # orange
    "#D9A21B",  # yellow
    "#2E9E5B",  # green
    "#0E9AA7",  # teal
)


def _key(course_id: int) -> str:
    return f"course_color:{course_id}"


def set_color(conn: sqlite3.Connection, course_id: int, color: str) -> str:
    color = color.upper()
    if color not in SUBJECT_COLORS:
        raise ValueError(f"color must be one of {', '.join(SUBJECT_COLORS)}")
    db.kv_set(conn, _key(course_id), color)
    return color


def course_colors(conn: sqlite3.Connection) -> dict[int, str]:
    """Every course's color, giving any course without one the next unused palette color."""
    courses = db.list_courses(conn)
    out = {c.id: db.kv_get(conn, _key(c.id)) for c in courses}
    used = {v for v in out.values() if v}
    for i, c in enumerate(courses):
        if out[c.id] in SUBJECT_COLORS:
            continue
        free = [x for x in SUBJECT_COLORS if x not in used]
        # more than 8 courses: go round the palette again
        out[c.id] = set_color(conn, c.id, free[0] if free else SUBJECT_COLORS[i % len(SUBJECT_COLORS)])
        used.add(out[c.id])
    return out
