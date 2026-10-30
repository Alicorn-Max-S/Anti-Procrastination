"""SQLite schema and the small data-access layer every module uses to read/write state.

Rows are stored as JSON for the nested bits (comments, rubric, card) to keep the schema small;
query columns (ids, due_at, status) are real columns. Each thread opens its own connection.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.models import Assignment, Block, Course, Ping, PrepCard, TestRow

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
CREATE TABLE IF NOT EXISTS test_rows (   -- filled by grader.py: one row per graded question
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    question TEXT, student_answer TEXT, marks REAL, marks_possible REAL,
    teacher_note TEXT, topic TEXT, qtype TEXT
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


def list_assignments(conn: sqlite3.Connection, course_id: int | None = None) -> list[Assignment]:
    sql = "SELECT data FROM assignments"
    args: tuple = ()
    if course_id is not None:
        sql += " WHERE course_id = ?"
        args = (course_id,)
    rows = conn.execute(sql + " ORDER BY due_at IS NULL, due_at", args).fetchall()
    return [Assignment.model_validate_json(r["data"]) for r in rows]


def get_assignment(conn: sqlite3.Connection, assignment_id: int) -> Assignment | None:
    row = conn.execute("SELECT data FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    return Assignment.model_validate_json(row["data"]) if row else None


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
    values = (b.assignment_id, b.start.isoformat(), b.end.isoformat(), b.status, int(b.at_risk))
    if b.id is None:
        cur = conn.execute(
            "INSERT INTO blocks (assignment_id, start, end, status, at_risk) VALUES (?, ?, ?, ?, ?)",
            values,
        )
        conn.commit()
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE blocks SET assignment_id=?, start=?, end=?, status=?, at_risk=? WHERE id=?",
        (*values, b.id),
    )
    conn.commit()
    return b.id


def list_blocks(conn: sqlite3.Connection) -> list[Block]:
    rows = conn.execute("SELECT * FROM blocks ORDER BY start").fetchall()
    return [Block.model_validate(dict(r)) for r in rows]


def get_block(conn: sqlite3.Connection, block_id: int) -> Block | None:
    row = conn.execute("SELECT * FROM blocks WHERE id = ?", (block_id,)).fetchone()
    return Block.model_validate(dict(row)) if row else None


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
    "teacher_note", "topic", "qtype",
)


def save_test_rows(conn: sqlite3.Connection, rows: list[TestRow]) -> list[int]:
    ids = []
    for r in rows:
        cur = conn.execute(
            f"INSERT INTO test_rows ({', '.join(_ROW_FIELDS)}) VALUES ({', '.join('?' * len(_ROW_FIELDS))})",
            tuple(getattr(r, f) for f in _ROW_FIELDS),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    return ids


def update_test_row(conn: sqlite3.Connection, r: TestRow) -> None:
    conn.execute(
        f"UPDATE test_rows SET {', '.join(f + '=?' for f in _ROW_FIELDS)} WHERE id=?",
        (*(getattr(r, f) for f in _ROW_FIELDS), r.id),
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
    return [TestRow.model_validate({k: v for k, v in dict(r).items() if v is not None}) for r in rows]


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
