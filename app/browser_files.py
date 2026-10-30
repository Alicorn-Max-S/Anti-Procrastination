"""Google files fetched by the Chrome extension (extension/) in the student's signed-in browser.

Most Google Docs/Slides/Drive files linked from Canvas are shared only inside the school's Google
domain, so the server can't download them (gdocs.py). The extension asks `needed()` which ones
are missing, downloads each with the student's own Google sign-in, and posts the bytes to
`save_upload()`. Files are stored once per Google file id under FILES_DIR/browser, named the way
gdocs.py names its downloads, and the document reader picks them up through `stored()` and
`copies()`. When the extension can't get a file it says why (`save_failure()`), and `reason()`
turns that into words for the card's "Open this doc".

Google Assignments copies never appear in Canvas. The extension reports docs opened from a Canvas
assignment tab, and `opened()` decides whether that's the student's copy for that assignment.

Only Google links that came from Canvas descriptions of upcoming work (or such a copy) are ever
asked for or accepted, and every call from the extension carries the key from `key()`.
"""

import hashlib
import hmac
import io
import re
import secrets
import sqlite3
import sys
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel

from app import canvas, db, gdocs
from app.config import settings
from app.models import Assignment

MAX_BYTES = 100_000_000  # same cap as gdocs.py and Canvas files
MAX_NEEDED = 30  # files per list; the extension fetches them one at a time
RETRY_AFTER = {"restricted": timedelta(days=1), "no_access": timedelta(days=1), "not_found": timedelta(days=1)}
RETRY_DEFAULT = timedelta(hours=1)

# Why the extension couldn't get a file, in words for the card.
REASONS = {
    "signin": "Chrome isn't signed in to your school Google account",
    "no_access": "your school Google account can't open it",
    "restricted": "the teacher turned off downloading, so open it to read it",
    "too_big": "over 100 MB",
    "not_found": "deleted, or the link is wrong",
    "not_a_file": "Google sent a web page instead of the file",
    "network": "couldn't reach Google",
    "error": "the download failed",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_files (   -- Google files the Chrome extension fetched, or couldn't
    file_id TEXT PRIMARY KEY,                 -- Google's file id
    url TEXT NOT NULL,                        -- the link as Canvas had it, or the opened copy
    assignment_id INTEGER,                    -- set for Google Assignments copies only
    path TEXT,                                -- the saved file, NULL until one arrives
    title TEXT NOT NULL DEFAULT '',
    sha256 TEXT,
    status TEXT NOT NULL,                     -- stored | failed
    reason TEXT NOT NULL DEFAULT '',          -- a REASONS key when failed
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""

_CANVAS_ASSIGNMENT = re.compile(r"/courses/(\d+)/assignments/(\d+)")


class Ref(BaseModel):
    """A Google file a Canvas link points at, and where the extension downloads it from."""

    file_id: str
    kind: str  # document | presentation | spreadsheets | drive
    fetch_url: str
    view_url: str  # the page the student opens; the extension loads it to tell no access from no download
    prefix: str  # file-name prefix, matching gdocs.py (the reader treats google-slides- PDFs as slides)


class Stored(BaseModel):
    file_id: str
    url: str
    path: Path
    title: str = ""
    assignment_id: int | None = None


def ref(url: str) -> Ref | None:
    """The Google file behind a link, or None for anything gdocs.py doesn't treat as one."""
    file_id = gdocs.file_id(url)
    export = gdocs.export_url(url)
    if not file_id or not export:
        return None
    for kind in ("document", "presentation", "spreadsheets"):
        if f"docs.google.com/{kind}/" in export:
            return Ref(
                file_id=file_id, kind=kind, fetch_url=export,
                view_url=f"https://docs.google.com/{kind}/d/{file_id}/edit",
                prefix="google-slides" if kind == "presentation" else "google",
            )
    # confirm=t skips Drive's "can't scan for viruses" page; the extension still stops at MAX_BYTES.
    return Ref(
        file_id=file_id, kind="drive",
        fetch_url=f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
        view_url=f"https://drive.google.com/file/d/{file_id}/view", prefix="google",
    )


# ---- storage ----------------------------------------------------------------------------------


def _ensure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    return conn


def _row(conn: sqlite3.Connection, file_id: str) -> sqlite3.Row | None:
    return _ensure(conn).execute("SELECT * FROM browser_files WHERE file_id = ?", (file_id,)).fetchone()


def _folder() -> Path:
    return Path(settings.files_dir) / "browser"


def _stored(row: sqlite3.Row | None) -> Stored | None:
    if row is None or row["status"] != "stored" or not row["path"] or not Path(row["path"]).exists():
        return None
    return Stored(
        file_id=row["file_id"], url=row["url"], path=Path(row["path"]), title=row["title"],
        assignment_id=row["assignment_id"],
    )


def stored(url: str, conn: sqlite3.Connection | None = None) -> Stored | None:
    """The file the extension saved for this Google link (any form of it), or None.
    For the document reader: check this before trying to download the link."""
    r = ref(url)
    if r is None:
        return None
    if conn is not None:
        return _stored(_row(conn, r.file_id))
    with closing(db.connect()) as own:
        return _stored(_row(own, r.file_id))


def copies(assignment_id: int, conn: sqlite3.Connection | None = None) -> list[Stored]:
    """Google Assignments copies the extension saved for this assignment (usually one)."""

    def query(c: sqlite3.Connection) -> list[Stored]:
        rows = _ensure(c).execute(
            "SELECT * FROM browser_files WHERE assignment_id = ? ORDER BY updated_at", (assignment_id,)
        ).fetchall()
        return [s for s in map(_stored, rows) if s is not None]

    if conn is not None:
        return query(conn)
    with closing(db.connect()) as own:
        return query(own)


def reason(url: str, conn: sqlite3.Connection | None = None) -> str:
    """Why the extension couldn't get this link, in words for the card; "" if it hasn't failed."""
    r = ref(url)
    if r is None:
        return ""

    def query(c: sqlite3.Connection) -> str:
        row = _row(c, r.file_id)
        return REASONS.get(row["reason"], "") if row is not None and row["status"] == "failed" else ""

    if conn is not None:
        return query(conn)
    with closing(db.connect()) as own:
        return query(own)


# ---- the shared key ---------------------------------------------------------------------------


def key(conn: sqlite3.Connection) -> str:
    """The key the extension sends with every call. Made on first use and kept in the DB."""
    value = db.kv_get(conn, "extension_key")
    if not value:
        value = secrets.token_urlsafe(24)
        db.kv_set(conn, "extension_key", value)
    return value


def key_ok(conn: sqlite3.Connection, given: str | None) -> bool:
    value = db.kv_get(conn, "extension_key")
    return bool(value and given) and hmac.compare_digest(value.encode(), given.encode())


# ---- what the extension should fetch ----------------------------------------------------------


def uses_google_assignments(a: Assignment) -> bool:
    return "assignments.google.com" in a.external_tool_url


def upcoming(conn: sqlite3.Connection, now: datetime) -> list[Assignment]:
    """Unsubmitted work due within the card horizon, soonest first: what cards get built for."""
    end = now + timedelta(days=settings.card_horizon_days)
    out = [a for a in db.list_assignments(conn) if not a.submitted and a.due_at and now < a.due_at <= end]
    return sorted(out, key=lambda a: a.due_at)


def _links(conn: sqlite3.Connection, now: datetime) -> dict[str, tuple[Ref, str, str, list[Assignment]]]:
    """Every Google file linked from upcoming work: file id -> (ref, link url, label, assignments)."""
    out: dict[str, tuple[Ref, str, str, list[Assignment]]] = {}
    for a in upcoming(conn, now):
        _, links, _ = canvas.parse_description(a.description_html)
        for link in links:
            r = ref(link.url)
            if r is None:
                continue
            if r.file_id not in out:
                out[r.file_id] = (r, link.url, link.label, [])
            if a not in out[r.file_id][3]:
                out[r.file_id][3].append(a)
    return out


def _read_by_card(conn: sqlite3.Connection, file_id: str, assignments: list[Assignment]) -> bool:
    """True when every card using this file already read it (it was shared publicly), so fetching
    it again would only trigger a rebuild for nothing."""
    for a in assignments:
        card = db.get_card(conn, a.id)
        if card is None:
            return False
        if not any(s.read and gdocs.file_id(s.url) == file_id for s in card.sources):
            return False
    return True


def _waiting(row: sqlite3.Row | None, now: datetime) -> bool:
    """True while a failed file is resting before the next try."""
    if row is None or row["status"] != "failed":
        return False
    when = datetime.fromisoformat(row["updated_at"])
    return now - when < RETRY_AFTER.get(row["reason"], RETRY_DEFAULT)


def needed(conn: sqlite3.Connection, now: datetime) -> dict:
    """What the extension should fetch now: Google files linked from upcoming work that nobody has
    read yet, and Google Assignments that are still missing the student's copy."""
    files = []
    for file_id, (r, url, label, assignments) in _links(conn, now).items():
        row = _row(conn, file_id)
        if _stored(row) or _waiting(row, now) or _read_by_card(conn, file_id, assignments):
            continue
        files.append({
            **r.model_dump(), "url": url, "name": label,
            "assignments": [{"id": a.id, "name": a.name} for a in assignments],
            "last_failure": row["reason"] if row is not None and row["status"] == "failed" else "",
        })
    courses = {c.id: c.name for c in db.list_courses(conn)}
    waiting_copies = [
        {"assignment_id": a.id, "name": a.name, "course": courses.get(a.course_id, ""), "html_url": a.html_url}
        for a in upcoming(conn, now)
        if uses_google_assignments(a) and not copies(a.id, conn)
    ]
    return {"files": files[:MAX_NEEDED], "copies": waiting_copies}


def _copy_target(conn: sqlite3.Connection, assignment_id: int | None, linked: bool) -> Assignment | None:
    """The Google Assignments assignment a doc can be the student's copy for, or None."""
    if assignment_id is None or linked:
        return None  # a file the teacher linked is the teacher's, not the student's copy
    a = db.get_assignment(conn, assignment_id)
    if a is None or a.submitted or not uses_google_assignments(a):
        return None
    return a


def opened(conn: sqlite3.Connection, url: str, opener_url: str, now: datetime) -> dict:
    """The student opened a Google doc. Should the extension save it, and for which assignment?

    Yes when upcoming work links it and it isn't saved yet (even if an earlier try failed: now
    the doc is open, the extension can also fetch it from the page). Also yes when it was opened
    from a Canvas assignment that uses Google Assignments and has no copy yet: that's the copy.
    """
    r = ref(url)
    if r is None:
        return {"wanted": False}
    out = {**r.model_dump(), "url": url, "wanted": False, "assignment_id": None}
    if _stored(_row(conn, r.file_id)):
        return out
    links = _links(conn, now)
    if r.file_id in links:
        return {**out, "wanted": not _read_by_card(conn, r.file_id, links[r.file_id][3])}
    m = _CANVAS_ASSIGNMENT.search(opener_url or "")
    canvas_host = urlparse(settings.canvas_base_url).netloc
    if m and (not canvas_host or urlparse(opener_url).netloc == canvas_host):
        a = _copy_target(conn, int(m.group(2)), linked=False)
        if a is not None and not copies(a.id, conn):
            return {**out, "wanted": True, "assignment_id": a.id, "assignment": a.name}
    return out


# ---- what the extension sends back ------------------------------------------------------------


class UploadError(ValueError):
    """An upload the server won't keep; the message says why."""


_OFFICE_ZIP = {"word/": ".docx", "ppt/": ".pptx", "xl/": ".xlsx"}
_OLE = {".doc", ".ppt", ".xls"}


def _suffix(data: bytes, filename: str) -> str:
    """The file's real type from its first bytes. Raises UploadError for web pages and anything
    the reader can't use."""
    head = data[:16]
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except zipfile.BadZipFile:
            raise UploadError("a broken ZIP file")
        for folder, suffix in _OFFICE_ZIP.items():
            if any(n.startswith(folder) for n in names):
                return suffix
        raise UploadError("a ZIP file that isn't a Word, PowerPoint or Excel file")
    if head.startswith(b"\xd0\xcf\x11\xe0") and Path(filename).suffix.lower() in _OLE:
        return Path(filename).suffix.lower()
    text = data[:512].lstrip().lower()
    if text.startswith((b"<!doctype", b"<html", b"<")):
        raise UploadError("a web page, not a file (probably a Google sign-in or error page)")
    if Path(filename).suffix.lower() == ".txt":
        return ".txt"
    raise UploadError("not a file type Start Helper can read")


def _accept(conn: sqlite3.Connection, url: str, assignment_id: int | None, now: datetime) -> tuple[Ref, list[int]]:
    """Check an upload is for a file Start Helper asked for. Returns its ref and the assignments
    it belongs to."""
    r = ref(url)
    if r is None:
        raise UploadError("not a Google Docs, Slides, Sheets or Drive link")
    links = _links(conn, now)
    if r.file_id in links:
        return r, [a.id for a in links[r.file_id][3]]
    row = _row(conn, r.file_id)
    if row is not None and row["status"] == "stored" and row["assignment_id"] is not None:
        return r, [row["assignment_id"]]  # the same copy again
    a = _copy_target(conn, assignment_id, linked=False)
    if a is not None and not copies(a.id, conn):
        return r, [a.id]
    raise UploadError("not a file linked from upcoming Canvas work, or a Google Assignments copy")


def _mark_stale(conn: sqlite3.Connection, assignment_ids: list[int]) -> list[int]:
    """Flag the cards built without this file so the next sync rebuilds them once."""
    marked = []
    for aid in assignment_ids:
        card = db.get_card(conn, aid)
        if card is not None and "stale" in type(card).model_fields and not card.stale:
            db.save_card(conn, card.model_copy(update={"stale": True}))
            marked.append(aid)
    return marked


def save_upload(
    conn: sqlite3.Connection,
    url: str,
    data: bytes,
    filename: str = "",
    title: str = "",
    assignment_id: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Keep a file the extension downloaded. Cards that use it are marked for one rebuild, but
    only when the bytes are new, so fetching the same file again never costs a model call."""
    now = now or datetime.now(timezone.utc)
    if len(data) > MAX_BYTES:
        raise UploadError(f"too big ({len(data) / 1e6:.0f} MB)")
    if not data:
        raise UploadError("an empty file")
    r, assignment_ids = _accept(conn, url, assignment_id, now)
    suffix = _suffix(data, filename)
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{r.prefix}-{r.file_id}{suffix}"
    for old in folder.glob(f"*-{r.file_id}.*"):
        if old != path and old.name.split(".")[0].endswith(r.file_id):
            old.unlink(missing_ok=True)  # an earlier upload in another format, and its reader cache
    sha = hashlib.sha256(data).hexdigest()
    old = _row(conn, r.file_id)
    is_new = old is None or old["sha256"] != sha or not _stored(old)
    path.write_bytes(data)
    copy_for = assignment_id if r.file_id not in _links(conn, now) else None
    if old is not None and old["assignment_id"] is not None:
        copy_for = old["assignment_id"]
    name = title.strip() or Path(filename).stem or (old["title"] if old is not None else "")
    conn.execute(
        "INSERT OR REPLACE INTO browser_files "
        "(file_id, url, assignment_id, path, title, sha256, status, reason, detail, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'stored', '', '', ?)",
        (r.file_id, url, copy_for, str(path), name[:200], sha, now.isoformat()),
    )
    conn.commit()
    marked = _mark_stale(conn, assignment_ids) if is_new else []
    return {"file_id": r.file_id, "new": is_new, "assignments": assignment_ids, "cards_marked": marked}


def save_failure(
    conn: sqlite3.Connection, url: str, why: str, detail: str = "", now: datetime | None = None
) -> dict:
    """Record why the extension couldn't get a file. A file that's already saved stays saved."""
    now = now or datetime.now(timezone.utc)
    r = ref(url)
    if r is None:
        raise UploadError("not a Google Docs, Slides, Sheets or Drive link")
    if why not in REASONS:
        raise UploadError(f"unknown reason {why!r}")
    old = _row(conn, r.file_id)
    if _stored(old):
        return {"file_id": r.file_id, "kept": "stored"}
    conn.execute(
        "INSERT OR REPLACE INTO browser_files "
        "(file_id, url, assignment_id, path, title, sha256, status, reason, detail, updated_at) "
        "VALUES (?, ?, NULL, NULL, ?, NULL, 'failed', ?, ?, ?)",
        (r.file_id, url, old["title"] if old is not None else "", why, detail[:300], now.isoformat()),
    )
    conn.commit()
    return {"file_id": r.file_id, "reason": REASONS[why]}


if __name__ == "__main__":
    # `python -m app.browser_files key`: print the key to paste into the extension's options.
    if sys.argv[1:] != ["key"]:
        sys.exit("usage: python -m app.browser_files key")
    with closing(db.connect()) as c:
        db.init_db(c)
        print(key(c))
