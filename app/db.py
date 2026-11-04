"""SQLite schema and the small data-access layer every module uses to read/write state.

Rows are stored as JSON for the nested bits (comments, rubric, card) to keep the schema small;
query columns (ids, due_at, status) are real columns. Each thread opens its own connection.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.models import Assignment, Availability, Block, Course, GradedTest, Ping, PrepCard, TestRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL,
    due_at TEXT,
    data TEXT NOT NULL            -- full Assignment as JSON
);
CREATE TABLE IF NOT EXISTS prep_cards (
    assignment_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL            -- PrepCard as JSON
);
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    at_risk INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT 'none',
    picked_time TEXT,
    started_at TEXT
);
CREATE TABLE IF NOT EXISTS availability (  -- availability.py: what the student said they can / can't do
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,           -- 'off' (can't work), 'open' (can work, even outside the usual hours)
                                  -- or 'session' (a stretch for the work in `assignments`)
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    assignments TEXT NOT NULL DEFAULT '[]'  -- sessions: JSON list of assignment ids
);
CREATE TABLE IF NOT EXISTS added (  -- added.py: work the student put on the list by hand
    assignment_id INTEGER PRIMARY KEY,
    finish_by TEXT,               -- when they want it done (stands in for the due date when planning)
    added_at TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT ''  -- where it came from, e.g. 'module:COURSE:ITEM', so it isn't added twice
);
CREATE TABLE IF NOT EXISTS test_rows (   -- filled by grader.py: one row per graded question
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    question TEXT, student_answer TEXT, marks REAL, marks_possible REAL,
    teacher_note TEXT, topic TEXT, qtype TEXT,
    test_id INTEGER, section TEXT, correct INTEGER,
    bonus INTEGER NOT NULL DEFAULT 0, estimated INTEGER NOT NULL DEFAULT 0,
    credit TEXT, decisions TEXT   -- Glimmer's credit label, and each label's tier and confidence as JSON
);
CREATE TABLE IF NOT EXISTS graded_tests (  -- filled by grader.py: one per uploaded test
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL,   -- 0 until filed under a course
    sha TEXT,
    data TEXT NOT NULL            -- GradedTest as JSON
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    data TEXT NOT NULL            -- the browser's PushSubscription JSON
);
CREATE TABLE IF NOT EXISTS kv (       -- small app state: last sync time, clock offset, ...
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the first version of a table: (table, column, definition).
# init_db adds any that an older data/app.db is missing.
LATER_COLUMNS = [
    ("blocks", "at_risk", "INTEGER NOT NULL DEFAULT 0"),
    ("test_rows", "test_id", "INTEGER"),
    ("test_rows", "section", "TEXT"),
    ("test_rows", "correct", "INTEGER"),
    ("test_rows", "bonus", "INTEGER NOT NULL DEFAULT 0"),
    ("test_rows", "estimated", "INTEGER NOT NULL DEFAULT 0"),
    ("blocks", "pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("blocks", "session_id", "INTEGER"),
    ("availability", "assignments", "TEXT NOT NULL DEFAULT '[]'"),
    ("test_rows", "credit", "TEXT"),
    ("test_rows", "decisions", "TEXT"),
]


def connect() -> sqlite3.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI may open a request's connection on one worker thread and
    # use it on another. Each request/job still gets its own connection.
    conn = sqlite3.connect(settings.db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, definition in LATER_COLUMNS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute("PRAGMA journal_mode=WAL")  # background loop and requests write at the same time
    conn.commit()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---- courses / assignments --------------------------------------------------------------------


def save_course(conn: sqlite3.Connection, c: Course) -> None:
    conn.execute("INSERT OR REPLACE INTO courses (id, name) VALUES (?, ?)", (c.id, c.name))
    conn.commit()


def list_courses(conn: sqlite3.Connection) -> list[Course]:
    rows = conn.execute("SELECT id, name FROM courses ORDER BY name").fetchall()
    return [Course(id=r["id"], name=r["name"]) for r in rows]


def save_assignment(conn: sqlite3.Connection, a: Assignment) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO assignments (id, course_id, due_at, data) VALUES (?, ?, ?, ?)",
        (a.id, a.course_id, _iso(a.due_at), a.model_dump_json()),
    )
    conn.commit()


def _with_added(a: Assignment, finish_by: str | None) -> Assignment:
    """An assignment the student added by hand: its finish-by time stands in for the due date
    everywhere the planner looks, and an added Canvas assignment keeps Canvas's date beside it."""
    finish = datetime.fromisoformat(finish_by) if finish_by else None
    update: dict = {"added": True}
    if finish is not None:
        update["due_at"] = finish
        if a.origin == "canvas":
            update["canvas_due_at"] = a.due_at
    return a.model_copy(update=update)


def _load_assignments(conn: sqlite3.Connection, rows) -> list[Assignment]:
    added = {r["assignment_id"]: r["finish_by"] for r in conn.execute("SELECT assignment_id, finish_by FROM added")}
    out = []
    for r in rows:
        a = Assignment.model_validate_json(r["data"])
        out.append(_with_added(a, added[a.id]) if a.id in added else a)
    return out


def due_key(a: Assignment) -> tuple:
    """Soonest due first, no due date last. Sorted on real datetimes: the stored text carries
    different UTC offsets (Canvas's Z, local times, a clock change), so it doesn't sort as text."""
    return (a.due_at is None, a.due_at.timestamp() if a.due_at else 0.0, a.id)


def list_assignments(conn: sqlite3.Connection, course_id: int | None = None) -> list[Assignment]:
    sql = "SELECT data FROM assignments"
    args: tuple = ()
    if course_id is not None:
        sql += " WHERE course_id = ?"
        args = (course_id,)
    return sorted(_load_assignments(conn, conn.execute(sql, args).fetchall()), key=due_key)


def get_assignment(conn: sqlite3.Connection, assignment_id: int) -> Assignment | None:
    row = conn.execute("SELECT data FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    return _load_assignments(conn, [row])[0] if row else None


def delete_assignment(conn: sqlite3.Connection, assignment_id: int) -> None:
    """An added-by-hand item, with its card, blocks, their pings and its added row."""
    conn.execute("DELETE FROM pings WHERE block_id IN (SELECT id FROM blocks WHERE assignment_id = ?)", (assignment_id,))
    for table, column in (("blocks", "assignment_id"), ("prep_cards", "assignment_id"), ("added", "assignment_id"), ("assignments", "id")):
        conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (assignment_id,))
    conn.commit()


def next_manual_id(conn: sqlite3.Connection) -> int:
    """Ids for added-by-hand items count down from -1, so they never meet a Canvas id."""
    row = conn.execute("SELECT MIN(id) AS low FROM assignments").fetchone()
    return min(-1, (row["low"] or 0) - 1)


# ---- work added by hand -----------------------------------------------------------------------


def save_added(conn: sqlite3.Connection, assignment_id: int, finish_by: datetime | None, added_at: datetime, ref: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO added (assignment_id, finish_by, added_at, ref) VALUES (?, ?, ?, ?)",
        (assignment_id, _iso(finish_by), added_at.isoformat(), ref),
    )
    conn.commit()


def list_added(conn: sqlite3.Connection) -> dict[int, dict]:
    """assignment id -> {finish_by, added_at, ref}."""
    return {
        r["assignment_id"]: {"finish_by": r["finish_by"], "added_at": r["added_at"], "ref": r["ref"]}
        for r in conn.execute("SELECT * FROM added")
    }


def delete_added(conn: sqlite3.Connection, assignment_id: int) -> None:
    conn.execute("DELETE FROM added WHERE assignment_id = ?", (assignment_id,))
    conn.commit()


# ---- prep cards -------------------------------------------------------------------------------


def save_card(conn: sqlite3.Connection, card: PrepCard) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO prep_cards (assignment_id, data) VALUES (?, ?)",
        (card.assignment_id, card.model_dump_json()),
    )
    conn.commit()


def list_cards(conn: sqlite3.Connection) -> list[PrepCard]:
    rows = conn.execute("SELECT data FROM prep_cards").fetchall()
    return [PrepCard.model_validate_json(r["data"]) for r in rows]


def get_card(conn: sqlite3.Connection, assignment_id: int) -> PrepCard | None:
    row = conn.execute(
        "SELECT data FROM prep_cards WHERE assignment_id = ?", (assignment_id,)
    ).fetchone()
    return PrepCard.model_validate_json(row["data"]) if row else None


# ---- blocks -----------------------------------------------------------------------------------


def save_block(conn: sqlite3.Connection, b: Block) -> int:
    """Insert a block, or update it if it already has an id. Returns the id."""
    values = (b.assignment_id, b.start.isoformat(), b.end.isoformat(), b.status, int(b.at_risk), int(b.pinned), b.session_id)
    if b.id is None:
        cur = conn.execute(
            "INSERT INTO blocks (assignment_id, start, end, status, at_risk, pinned, session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        conn.commit()
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE blocks SET assignment_id=?, start=?, end=?, status=?, at_risk=?, pinned=?, session_id=? WHERE id=?",
        (*values, b.id),
    )
    conn.commit()
    return b.id


def list_blocks(conn: sqlite3.Connection) -> list[Block]:
    """In time order (sorted on real datetimes, not the stored text: see due_key)."""
    rows = conn.execute("SELECT * FROM blocks").fetchall()
    return sorted((Block.model_validate(dict(r)) for r in rows), key=lambda b: (b.start.timestamp(), b.id))


def get_block(conn: sqlite3.Connection, block_id: int) -> Block | None:
    row = conn.execute("SELECT * FROM blocks WHERE id = ?", (block_id,)).fetchone()
    return Block.model_validate(dict(row)) if row else None


def delete_block(conn: sqlite3.Connection, block_id: int) -> None:
    conn.execute("DELETE FROM blocks WHERE id = ?", (block_id,))
    conn.commit()


# ---- availability -----------------------------------------------------------------------------


def save_availability(conn: sqlite3.Connection, a: Availability) -> int:
    """Insert a span (no id) or update it; returns the id."""
    values = (a.kind, a.start.isoformat(), a.end.isoformat(), a.note, json.dumps(a.assignment_ids))
    if a.id is None:
        cur = conn.execute("INSERT INTO availability (kind, start, end, note, assignments) VALUES (?, ?, ?, ?, ?)", values)
        conn.commit()
        return int(cur.lastrowid)
    conn.execute("UPDATE availability SET kind=?, start=?, end=?, note=?, assignments=? WHERE id=?", (*values, a.id))
    conn.commit()
    return a.id


def _span(row) -> Availability:
    d = dict(row)
    d["assignment_ids"] = json.loads(d.pop("assignments", None) or "[]")
    return Availability.model_validate(d)


def list_availability(conn: sqlite3.Connection, since: datetime | None = None) -> list[Availability]:
    """Spans in time order; with `since`, only those that haven't ended by then. (Filtered here, not in
    SQL: the stored times carry different UTC offsets across a clock change, so they don't sort as text.)"""
    rows = conn.execute("SELECT * FROM availability").fetchall()
    spans = [_span(r) for r in rows]
    return sorted((a for a in spans if since is None or a.end > since), key=lambda a: a.start)


def get_availability(conn: sqlite3.Connection, availability_id: int) -> Availability | None:
    row = conn.execute("SELECT * FROM availability WHERE id = ?", (availability_id,)).fetchone()
    return _span(row) if row else None


def delete_availability(conn: sqlite3.Connection, availability_id: int) -> None:
    conn.execute("DELETE FROM availability WHERE id = ?", (availability_id,))
    conn.commit()


# ---- pings ------------------------------------------------------------------------------------


def save_ping(conn: sqlite3.Connection, p: Ping) -> int:
    values = (p.block_id, p.sent_at.isoformat(), p.answer, _iso(p.picked_time), _iso(p.started_at))
    if p.id is None:
        cur = conn.execute(
            "INSERT INTO pings (block_id, sent_at, answer, picked_time, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            values,
        )
        conn.commit()
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE pings SET block_id=?, sent_at=?, answer=?, picked_time=?, started_at=? WHERE id=?",
        (*values, p.id),
    )
    conn.commit()
    return p.id


def latest_ping(conn: sqlite3.Connection, block_id: int) -> Ping | None:
    row = conn.execute(
        "SELECT * FROM pings WHERE block_id = ? ORDER BY id DESC LIMIT 1", (block_id,)
    ).fetchone()
    return Ping.model_validate(dict(row)) if row else None


def list_pings(conn: sqlite3.Connection) -> list[Ping]:
    rows = conn.execute("SELECT * FROM pings ORDER BY sent_at").fetchall()
    return [Ping.model_validate(dict(r)) for r in rows]


# ---- graded test rows -------------------------------------------------------------------------

_ROW_FIELDS = (
    "course_id", "source", "question", "student_answer", "marks", "marks_possible",
    "teacher_note", "topic", "qtype", "test_id", "section", "correct", "bonus", "estimated",
    "credit", "decisions",
)


def _row_values(r: TestRow) -> tuple:
    """A row's values in _ROW_FIELDS order; the decisions go in as JSON."""
    return tuple(json.dumps(r.decisions) if f == "decisions" else getattr(r, f) for f in _ROW_FIELDS)


def save_test_rows(conn: sqlite3.Connection, rows: list[TestRow]) -> list[int]:
    ids = []
    for r in rows:
        cur = conn.execute(
            f"INSERT INTO test_rows ({', '.join(_ROW_FIELDS)}) VALUES ({', '.join('?' * len(_ROW_FIELDS))})",
            _row_values(r),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    return ids


def update_test_row(conn: sqlite3.Connection, r: TestRow) -> None:
    conn.execute(
        f"UPDATE test_rows SET {', '.join(f + '=?' for f in _ROW_FIELDS)} WHERE id=?",
        (*_row_values(r), r.id),
    )
    conn.commit()


def delete_test_row(conn: sqlite3.Connection, row_id: int) -> None:
    conn.execute("DELETE FROM test_rows WHERE id = ?", (row_id,))
    conn.commit()


def list_test_rows(conn: sqlite3.Connection, course_id: int | None = None) -> list[TestRow]:
    sql = "SELECT * FROM test_rows"
    args: tuple = ()
    if course_id is not None:
        sql += " WHERE course_id = ?"
        args = (course_id,)
    rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    out = []
    for r in rows:
        data = {k: v for k, v in dict(r).items() if v is not None}
        data["decisions"] = json.loads(data["decisions"]) if data.get("decisions") else {}
        out.append(TestRow.model_validate(data))
    return out


def save_graded_test(conn: sqlite3.Connection, t: GradedTest) -> int:
    """Insert (no id) or replace a graded test; returns its id."""
    if t.id is None:
        cur = conn.execute("INSERT INTO graded_tests (course_id, sha, data) VALUES (?, ?, '{}')", (t.course_id, t.sha))
        t = t.model_copy(update={"id": int(cur.lastrowid)})
    conn.execute(
        "UPDATE graded_tests SET course_id = ?, sha = ?, data = ? WHERE id = ?",
        (t.course_id, t.sha, t.model_dump_json(), t.id),
    )
    conn.commit()
    return t.id


def get_graded_test(conn: sqlite3.Connection, test_id: int) -> GradedTest | None:
    row = conn.execute("SELECT data FROM graded_tests WHERE id = ?", (test_id,)).fetchone()
    return GradedTest.model_validate_json(row["data"]) if row else None


def list_graded_tests(conn: sqlite3.Connection, course_id: int | None = None) -> list[GradedTest]:
    sql = "SELECT data FROM graded_tests"
    args: tuple = ()
    if course_id is not None:
        sql += " WHERE course_id = ?"
        args = (course_id,)
    return [GradedTest.model_validate_json(r["data"]) for r in conn.execute(sql + " ORDER BY id", args)]


def delete_graded_test(conn: sqlite3.Connection, test_id: int) -> None:
    """The test and every row read from it."""
    conn.execute("DELETE FROM test_rows WHERE test_id = ?", (test_id,))
    conn.execute("DELETE FROM graded_tests WHERE id = ?", (test_id,))
    conn.commit()


def move_graded_test(conn: sqlite3.Connection, test_id: int, course_id: int) -> GradedTest | None:
    """File a test (and its rows) under another course."""
    t = get_graded_test(conn, test_id)
    if t is None:
        return None
    t = t.model_copy(update={"course_id": course_id})
    save_graded_test(conn, t)
    conn.execute("UPDATE test_rows SET course_id = ? WHERE test_id = ?", (course_id, test_id))
    conn.commit()
    return t


# ---- push subscriptions -----------------------------------------------------------------------


def save_subscription(conn: sqlite3.Connection, sub: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO push_subscriptions (endpoint, data) VALUES (?, ?)",
        (sub["endpoint"], json.dumps(sub)),
    )
    conn.commit()


def list_subscriptions(conn: sqlite3.Connection) -> list[dict]:
    return [json.loads(r["data"]) for r in conn.execute("SELECT data FROM push_subscriptions")]


def delete_subscription(conn: sqlite3.Connection, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.commit()


# ---- key/value state --------------------------------------------------------------------------


def kv_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
