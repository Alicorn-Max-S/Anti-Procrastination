"""Throwaway hand-labelling site. From the repo root: `python -m labeling`, then open
http://127.0.0.1:8100.

Reads the already-synced Canvas assignments from the app's database (read-only; nothing in the
app's tables changes and no model is called). Uploaded tests are shown as page images, with one
screen per question you say the test has. Everything you decide is appended to
data/labeling/labels.jsonl, one JSON object per line:
    {"item_id", "item_kind", "field", "label", "timestamp"}
The last row for an (item, field) wins, so changing your mind just appends. New topics are rows in
the same file (item_kind "course", field "topic_added"). data/ is gitignored: this repo is public.

/review lists every labeled assignment with its labels as menus, to look them over and change any
of them, with a one-click "Make it a quick task".
"""

import json
import shutil
import sqlite3
import threading
import zlib
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from app import db
from app.canvas import parse_description
from app.config import settings
from app.models import Assignment
from decisions.fields import (
    ASSIGNMENT_KIND,
    FIELDS_BY_KIND,
    TEST_QUESTION_KIND,
    field_values,
    normalize_topic,
)
from labeling.courses import is_academic

HOST, PORT = "127.0.0.1", 8100
ORDER_SEED = 31
MAX_PAGE_BYTES = 40_000_000
PAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf"}

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "labeling"
LABELS = DATA / "labels.jsonl"
LAST_YEAR = DATA / "last_year.json"
TESTS = DATA / "tests"  # tests/<test id>/meta.json plus the page files

_write_lock = threading.Lock()

app = FastAPI(title="Labeling", docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Loopback clients only, and only under a loopback name (blocks DNS-rebinding pages)."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
            or host not in {"127.0.0.1", "localhost", "::1"}):
        return JSONResponse({"detail": "local only"}, status_code=403)
    return await call_next(request)


# ---- the app's data, read-only ----------------------------------------------------------------


def _app_db() -> sqlite3.Connection:
    path = Path(settings.db_path)
    path = path if path.is_absolute() else ROOT / path
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        conn.execute("SELECT 1 FROM courses LIMIT 1")
    except sqlite3.Error:  # a WAL database with no -shm next to it can't open plain read-only
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


class _FileLinks(HTMLParser):
    """Names of the files linked from an assignment description (Canvas file links)."""

    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self._open = False
        self._text: list[str] = []
        self._fallback = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("data-api-returntype") == "File":
            self._open, self._text = True, []
            self._fallback = a.get("title") or a.get("data-api-endpoint") or ""

    def handle_data(self, data):
        if self._open:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            self._open = False
            name = " ".join("".join(self._text).split()) or self._fallback.rstrip("/").rsplit("/", 1)[-1]
            if name:
                self.names.append(name)


def attachment_names(a: Assignment) -> list[str]:
    folder = ROOT / settings.files_dir / "attachments" / str(a.id)
    if folder.is_dir():  # the app already downloaded them: those are the real names
        return sorted(p.name for p in folder.iterdir() if p.is_file())
    links = _FileLinks()
    links.feed(a.description_html or "")
    return list(dict.fromkeys(links.names))


def load_last_year() -> dict | None:
    """Last year's pool and its random sample, written by `python -m labeling.fetch_last_year`."""
    return json.loads(LAST_YEAR.read_text(encoding="utf-8")) if LAST_YEAR.exists() else None


def shuffled(ids: list[int]) -> list[int]:
    """A fixed pseudo-random order that doesn't shift when ids are added, so courses interleave."""
    return sorted(ids, key=lambda i: zlib.crc32(f"{ORDER_SEED}:{i}".encode()))


def assignment_item(a: Assignment, course: str) -> dict:
    """What the model will see of an assignment. No guessed kind, score or feedback."""
    due = a.due_at.astimezone().strftime("%a %b %d, %Y, %I:%M %p").replace(" 0", " ") if a.due_at else None
    return {
        "kind": ASSIGNMENT_KIND,
        "id": str(a.id),
        "key": f"{ASSIGNMENT_KIND}:{a.id}",
        "course_id": a.course_id,
        "course": course,
        "title": a.name,
        "due": due,
        "points": a.points_possible,
        "submission_types": a.submission_types,
        "attachments": attachment_names(a),
        "rubric": [{"criterion": r.criterion, "points": r.points_possible} for r in a.rubric],
        "description": parse_description(a.description_html)[0],
    }


# ---- uploaded tests ---------------------------------------------------------------------------


def list_tests() -> list[dict]:
    metas = [json.loads(p.read_text()) for p in TESTS.glob("*/meta.json")]
    return sorted(metas, key=lambda m: m["id"])


def question_items(test: dict, course: str) -> list[dict]:
    pages = [{"url": f"/files/{test['id']}/{f}", "pdf": f.endswith(".pdf")} for f in test["pages"]]
    return [
        {
            "kind": TEST_QUESTION_KIND,
            "id": f"{test['id']}:q{k}",
            "key": f"{TEST_QUESTION_KIND}:{test['id']}:q{k}",
            "test_id": test["id"],
            "course_id": test["course_id"],
            "course": course,
            "test": test["name"],
            "number": k,
            "of": test["questions"],
            "pages": pages,
        }
        for k in range(1, test["questions"] + 1)
    ]


# ---- labels.jsonl -----------------------------------------------------------------------------


def read_rows() -> list[dict]:
    if not LABELS.exists():
        return []
    rows = []
    for line in LABELS.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:  # a half-written last line after a crash
            continue
    return rows


def append_row(item_id: str, item_kind: str, field: str, label: str) -> None:
    row = {
        "item_id": item_id,
        "item_kind": item_kind,
        "field": field,
        "label": label,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    DATA.mkdir(parents=True, exist_ok=True)
    with _write_lock, LABELS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()


def current_labels(rows: list[dict]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for r in rows:
        if r["item_kind"] != "course":
            out.setdefault(f"{r['item_kind']}:{r['item_id']}", {})[r["field"]] = r["label"]
    return out


def label_history(rows: list[dict]) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """For the review page: each item's label before the current one, per field (what an undo
    goes back to), and when each item was last labeled."""
    seen: dict[str, dict[str, list[str]]] = {}
    last: dict[str, str] = {}
    for r in rows:
        if r["item_kind"] == "course":
            continue
        key = f"{r['item_kind']}:{r['item_id']}"
        labels = seen.setdefault(key, {}).setdefault(r["field"], [])
        if not labels or labels[-1] != r["label"]:
            labels.append(r["label"])
        last[key] = r.get("timestamp", "")
    previous = {k: {f: v[-2] for f, v in fields.items() if len(v) > 1} for k, fields in seen.items()}
    return previous, last


def class_topics(rows: list[dict], conn: sqlite3.Connection) -> dict[int, list[str]]:
    """Each class's topics: any already in the app's graded-test rows, then the ones added here."""
    topics: dict[int, list[str]] = {}
    for r in db.list_test_rows(conn):
        if r.topic and r.topic not in topics.setdefault(r.course_id, []):
            topics[r.course_id].append(r.topic)
    for r in rows:
        if r["item_kind"] == "course" and r["field"] == "topic_added":
            mine = topics.setdefault(int(r["item_id"]), [])
            if r["label"] not in mine:
                mine.append(r["label"])
    return topics


# ---- routes -----------------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def page() -> str:
    return (Path(__file__).parent / "page.html").read_text(encoding="utf-8")


@app.get("/review", response_class=HTMLResponse)
def review() -> str:
    return (Path(__file__).parent / "review.html").read_text(encoding="utf-8")


def course_names(conn: sqlite3.Connection, last_year: dict | None = None) -> dict[int, str]:
    """The academic classes, this year's from the app and last year's from last_year.json."""
    courses = {c.id: c.name for c in db.list_courses(conn) if is_academic(c.name)}
    last_year = last_year or load_last_year()
    if last_year:
        courses.update({int(k): v for k, v in last_year["courses"].items()})
    return courses


def build_items(conn: sqlite3.Connection) -> tuple[list[dict], dict[int, str]]:
    """Every current assignment in an academic class, last year's sample, then the test questions."""
    last_year = load_last_year()
    courses = course_names(conn, last_year)
    by_id = {a.id: a for a in db.list_assignments(conn) if a.course_id in courses}
    if last_year:
        for i in last_year["sample_ids"]:
            by_id[i] = Assignment.model_validate(last_year["assignments"][str(i)])
    items = [assignment_item(by_id[i], courses.get(by_id[i].course_id, "")) for i in shuffled(list(by_id))]
    for test in list_tests():
        items += question_items(test, courses.get(test["course_id"], ""))
    return items, courses


@app.get("/api/items")
def get_items() -> dict:
    conn = _app_db()
    try:
        items, courses = build_items(conn)
        rows = read_rows()
        previous, labeled_at = label_history(rows)
        return {
            "items": items,
            "labels": current_labels(rows),
            "previous": previous,
            "labeled_at": labeled_at,
            "topics": {str(k): v for k, v in class_topics(rows, conn).items()},
            "courses": [{"id": k, "name": v} for k, v in sorted(courses.items(), key=lambda kv: kv[1])],
            "fields": {
                kind: [
                    {"key": f.key, "question": f.question, "per_class": f.per_class,
                     "options": [{"value": o.value, "label": o.label} for o in f.options]}
                    for f in fields
                ]
                for kind, fields in FIELDS_BY_KIND.items()
            },
            "labels_path": str(LABELS),
        }
    finally:
        conn.close()


class LabelIn(BaseModel):
    item_id: str
    item_kind: str
    field: str
    label: str


@app.post("/api/label")
def post_label(body: LabelIn) -> dict:
    fields = {f.key: f for f in FIELDS_BY_KIND.get(body.item_kind, ())}
    field = fields.get(body.field)
    if field is None:
        raise HTTPException(400, "unknown item kind or field")
    conn = _app_db()
    try:
        items, _ = build_items(conn)
        item = next((i for i in items if i["kind"] == body.item_kind and i["id"] == body.item_id), None)
        if item is None:
            raise HTTPException(404, "unknown item")
        allowed = (class_topics(read_rows(), conn).get(item["course_id"], []) if field.per_class
                   else field_values(field))
    finally:
        conn.close()
    if body.label not in allowed:
        raise HTTPException(400, "not one of the options")
    append_row(body.item_id, body.item_kind, body.field, body.label)
    return {"ok": True}


class TopicIn(BaseModel):
    course_id: int
    name: str


@app.post("/api/topic")
def post_topic(body: TopicIn) -> dict:
    name = normalize_topic(body.name)
    if not name:
        raise HTTPException(400, "topic name is empty")
    conn = _app_db()
    try:
        if body.course_id not in course_names(conn):
            raise HTTPException(404, "unknown class")
        if name not in class_topics(read_rows(), conn).get(body.course_id, []):
            append_row(str(body.course_id), "course", "topic_added", name)
        return {"topic": name, "topics": class_topics(read_rows(), conn).get(body.course_id, [])}
    finally:
        conn.close()


@app.post("/api/tests")
async def upload_test(
    course_id: int = Form(...),
    name: str = Form(""),
    questions: int = Form(...),
    files: list[UploadFile] = File(...),
) -> dict:
    conn = _app_db()
    try:
        if course_id not in course_names(conn):
            raise HTTPException(404, "unknown class")
    finally:
        conn.close()
    if not 1 <= questions <= 300:
        raise HTTPException(400, "questions must be between 1 and 300")
    if not files:
        raise HTTPException(400, "no files")
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in PAGE_TYPES:
            raise HTTPException(400, f"{f.filename}: use png, jpg, webp, gif or pdf (convert HEIC to JPG first)")

    TESTS.mkdir(parents=True, exist_ok=True)
    taken = [int(p.name.split("-")[1]) for p in TESTS.glob("test-*") if p.name.split("-")[1].isdigit()]
    test_id = f"test-{max(taken, default=0) + 1:03d}"
    folder = TESTS / test_id
    folder.mkdir()
    try:
        pages = []
        for n, f in enumerate(files, 1):
            fname = f"page-{n:02d}{Path(f.filename or '').suffix.lower()}"
            data = await f.read(MAX_PAGE_BYTES + 1)
            if len(data) > MAX_PAGE_BYTES:
                raise HTTPException(400, f"{f.filename} is over {MAX_PAGE_BYTES // 1_000_000} MB")
            (folder / fname).write_bytes(data)
            pages.append(fname)
        meta = {
            "id": test_id,
            "name": name.strip() or Path(files[0].filename or test_id).stem,
            "course_id": course_id,
            "questions": questions,
            "pages": pages,
            "original_names": [f.filename for f in files],
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (folder / "meta.json").write_text(json.dumps(meta, indent=2))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {"id": test_id}


@app.post("/api/tests/{test_id}/questions")
def add_question(test_id: str) -> dict:
    meta_path = TESTS / test_id / "meta.json"
    if "/" in test_id or "\\" in test_id or not meta_path.is_file():
        raise HTTPException(404, "unknown test")
    with _write_lock:
        meta = json.loads(meta_path.read_text())
        meta["questions"] += 1
        meta_path.write_text(json.dumps(meta, indent=2))
    return {"questions": meta["questions"]}


@app.get("/files/{test_id}/{filename}")
def get_page(test_id: str, filename: str) -> FileResponse:
    path = (TESTS / test_id / filename).resolve()
    if TESTS.resolve() not in path.parents or not path.is_file() or path.suffix.lower() not in PAGE_TYPES:
        raise HTTPException(404)
    return FileResponse(path)
