"""Google files fetched by the Chrome extension (extension/) in the student's signed-in browser.

Most Google Docs/Slides/Drive files linked from Canvas are shared only inside the school's Google
domain, so the server can't download them (gdocs.py). The extension asks `needed()` which ones
are missing, downloads each with the student's own Google sign-in, and posts the bytes to
`save_upload()`. Files are stored once per Google file id under FILES_DIR/browser, named the way
gdocs.py names its downloads, and the document reader picks them up through `stored()` and
`copies()`. When the extension can't get a file it says why (`save_failure()`), and `reason()`
turns that into words for the card's "Open this doc".

When the teacher turned off downloading, Max wants the doc read anyway (his call, Oct 30: that's
usually a teacher's mistake). The extension then sends the doc's page view instead (Docs'
/mobilebasic, Slides' /htmlpresent, Sheets' /htmlview/sheet); its text is kept as a .txt and the
row says `via = "page"` so the card can tell the student where the text came from.

Google Assignments copies never appear in Canvas. The extension reports docs opened from a Canvas
assignment tab, and `opened()` decides whether that's the student's copy for that assignment.

Only Google links that came from Canvas descriptions of upcoming work (or such a copy) are ever
asked for or accepted, and every call from the extension carries the key from `key()`.
"""

import hashlib
import hmac
import io
import re
from html.parser import HTMLParser
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
    updated_at TEXT NOT NULL,
    via TEXT NOT NULL DEFAULT 'download'      -- download | page (text from the page view; downloading was off)
);
"""
_LATER_COLUMNS = [("via", "TEXT NOT NULL DEFAULT 'download'")]
_ready: set[str] = set()  # DB files whose table is known to be current

PAGE_VIEW_NOTE = "(Read from the page view: the teacher turned off downloading.)"

_CANVAS_ASSIGNMENT = re.compile(r"/courses/(\d+)/assignments/(\d+)")

# The server-rendered view of each kind of Google file, with its text in the HTML. Plain
# /htmlview for Sheets is drawn by JavaScript now; /htmlview/sheet has the cells (first tab only).
_PAGE_VIEWS = {
    "document": "mobilebasic",
    "presentation": "htmlpresent",
    "spreadsheets": "htmlview/sheet?headers=false&gid=0",
}


class Ref(BaseModel):
    """A Google file a Canvas link points at, and where the extension downloads it from."""

    file_id: str
    kind: str  # document | presentation | spreadsheets | drive
    fetch_url: str
    view_url: str  # the page the student opens; the extension loads it to tell no access from no download
    page_url: str = ""  # server-rendered page view, read when downloading is turned off ("" for Drive files)
    prefix: str  # file-name prefix, matching gdocs.py (the reader treats google-slides- PDFs as slides)


class Stored(BaseModel):
    file_id: str
    url: str
    path: Path
    title: str = ""
    assignment_id: int | None = None
    via: str = "download"  # "page" when the text came from the page view because downloading was off


def ref(url: str) -> Ref | None:
    """The Google file behind a link, or None for anything gdocs.py doesn't treat as one."""
    file_id = gdocs.file_id(url)
    export = gdocs.export_url(url)
    if not file_id or not export:
        return None
    for kind, page in _PAGE_VIEWS.items():
        if f"docs.google.com/{kind}/" in export:
            return Ref(
                file_id=file_id, kind=kind, fetch_url=export,
                view_url=f"https://docs.google.com/{kind}/d/{file_id}/edit",
                page_url=f"https://docs.google.com/{kind}/d/{file_id}/{page}",
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
    """Create the table, or add columns an older version of it lacks (once per DB file)."""
    where = conn.execute("PRAGMA database_list").fetchone()["file"]  # "" for an in-memory DB
    if where and where in _ready:
        return conn
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(browser_files)")}
    for column, definition in _LATER_COLUMNS:
        if column not in have:
            conn.execute(f"ALTER TABLE browser_files ADD COLUMN {column} {definition}")
    conn.commit()
    _ready.add(where)
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
        assignment_id=row["assignment_id"], via=row["via"],
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


def note_contact(conn: sqlite3.Connection, key_was_ok: bool) -> None:
    """Remember the extension's last call, or the last one with a wrong key, for status()."""
    db.kv_set(conn, "ext_seen" if key_was_ok else "ext_bad_key", datetime.now(timezone.utc).isoformat())


# ---- status for the dashboard -----------------------------------------------------------------

SEEN_RECENTLY = timedelta(minutes=25)  # the extension checks in every 10 minutes while Chrome runs


def _card_use(card, file_id: str) -> str:
    """How one card stands with a fetched file: read | rebuilding | not_read | no_card."""
    if card is None:
        return "no_card"
    if card.stale:
        return "rebuilding"
    if any(s.read and gdocs.file_id(s.url) == file_id for s in card.sources):
        return "read"
    return "not_read"


def status(conn: sqlite3.Connection, now: datetime) -> dict:
    """What the extension has done, for the dashboard: when it last checked in (and the last call
    with a wrong key), every file it saved or couldn't get and why, which cards use each one and
    whether they've been rebuilt with it, and what it still has to fetch."""
    links = _links(conn, now)
    names = {a.id: a.name for a in db.list_assignments(conn)}
    cards = {c.assignment_id: c for c in db.list_cards(conn)}
    files = []
    for row in _ensure(conn).execute("SELECT * FROM browser_files ORDER BY updated_at DESC").fetchall():
        ids = [a.id for a in links[row["file_id"]][3]] if row["file_id"] in links else []
        if row["assignment_id"] is not None and row["assignment_id"] not in ids:
            ids.append(row["assignment_id"])
        files.append({
            "title": row["title"] or row["url"],
            "url": row["url"],
            "status": row["status"],
            "reason": REASONS.get(row["reason"], row["reason"]) if row["status"] == "failed" else "",
            "detail": row["detail"],
            "via": row["via"],
            "at": row["updated_at"],
            "copy_for": names.get(row["assignment_id"]) if row["assignment_id"] is not None else None,
            "cards": [
                {"assignment_id": aid, "name": names.get(aid, f"Assignment {aid}"), "use": _card_use(cards.get(aid), row["file_id"])}
                for aid in ids
            ],
        })
    todo = needed(conn, now)
    seen = db.kv_get(conn, "ext_seen")
    bad = db.kv_get(conn, "ext_bad_key")
    real_now = datetime.now(timezone.utc)
    return {
        "seen_at": seen,
        "recent": bool(seen) and real_now - datetime.fromisoformat(seen) < SEEN_RECENTLY,
        "bad_key_at": bad if bad and (not seen or bad > seen) else None,
        "files": files,
        "waiting": [f["name"] or f["url"] for f in todo["files"]],
        "waiting_copies": [c["name"] for c in todo["copies"]],
    }


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
    read yet, and Google Assignments that are still missing the student's copy. `watch` is every
    unread file id, including ones resting after a failure: if the student opens one, the
    extension tries it then (and only reports opened docs that are on this list or were opened
    from a Canvas assignment)."""
    files = []
    watch = []
    for file_id, (r, url, label, assignments) in _links(conn, now).items():
        row = _row(conn, file_id)
        if _stored(row) or _read_by_card(conn, file_id, assignments):
            continue
        watch.append(file_id)
        if _waiting(row, now):
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
    return {"files": files[:MAX_NEEDED], "watch": watch, "copies": waiting_copies}


def _copy_target(conn: sqlite3.Connection, assignment_id: int | None, linked: bool) -> Assignment | None:
    """The Google Assignments assignment a doc can be the student's copy for, or None."""
    if assignment_id is None or linked:
        return None  # a file the teacher linked is the teacher's, not the student's copy
    a = db.get_assignment(conn, assignment_id)
    if a is None or a.submitted or not uses_google_assignments(a):
        return None
    return a


def opened(
    conn: sqlite3.Connection, url: str, opener_url: str, now: datetime, assignment_id: int | None = None
) -> dict:
    """The student opened a Google doc. Should the extension save it, and for which assignment?

    Yes when upcoming work links it and it isn't saved yet (even if an earlier try failed: now
    the doc is open, the extension can also fetch it from the page). Also yes when it was opened
    from a Canvas assignment that uses Google Assignments and has no copy yet, or the student
    picked that assignment in the popup: that's the student's copy.
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
    if assignment_id is None:
        m = _CANVAS_ASSIGNMENT.search(opener_url or "")
        canvas_host = urlparse(settings.canvas_base_url).netloc
        if m and (not canvas_host or urlparse(opener_url).netloc == canvas_host):
            assignment_id = int(m.group(2))
    a = _copy_target(conn, assignment_id, linked=False)
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


class _PageText(HTMLParser):
    """The readable text of a Google page view: block elements become line breaks, scripts and
    styles are dropped. Slides come as SVG, one <svg> per slide and a <text> per line."""

    _SKIP = {"script", "style", "noscript", "head", "template"}
    _BLOCKS = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section",
        "article", "ul", "ol", "blockquote", "pre", "hr", "text",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self._SKIP:
            self._skip += 1
        elif tag == "svg":
            self.parts.append("\n\n")  # a new slide
        elif tag in ("td", "th"):
            self.parts.append("\t")
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def page_text(html: str) -> tuple[str, str]:
    """(title, text) of a Docs/Slides/Sheets page view."""
    parser = _PageText()
    parser.feed(html)
    text = re.sub(r"[ \t\xa0]+", " ", "".join(parser.parts))
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title = re.sub(r"\s*-\s*Google (Docs|Slides|Sheets|Drive)\s*$", "", parser.title.strip())
    return title, text


def _from_page(r: Ref, data: bytes) -> tuple[str, bytes]:
    """The text file to keep for a page view the extension sent, and the page's title."""
    if r.kind not in _PAGE_VIEWS:
        raise UploadError("Drive files have no page view to read")
    title, text = page_text(data.decode("utf-8", errors="replace"))
    if len(text) < 40:
        raise UploadError("the page view had no text in it")
    return title, f"{title}\n{PAGE_VIEW_NOTE}\n\n{text}\n".encode("utf-8")


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
        if card is not None and not card.stale:
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
    via: str = "download",
) -> dict:
    """Keep a file the extension downloaded, or the text of a page view when downloading was
    turned off (`via="page"`). Cards that use it are marked for one rebuild, but only when the
    content is new, so fetching the same file again never costs a model call."""
    now = now or datetime.now(timezone.utc)
    if len(data) > MAX_BYTES:
        raise UploadError(f"too big ({len(data) / 1e6:.0f} MB)")
    if not data:
        raise UploadError("an empty file")
    if via not in ("download", "page"):
        raise UploadError(f"unknown way of getting it {via!r}")
    r, assignment_ids = _accept(conn, url, assignment_id, now)
    if via == "page":
        page_title, data = _from_page(r, data)
        title = title or page_title
        suffix = ".txt"
    else:
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
        "(file_id, url, assignment_id, path, title, sha256, status, reason, detail, updated_at, via) "
        "VALUES (?, ?, ?, ?, ?, ?, 'stored', '', '', ?, ?)",
        (r.file_id, url, copy_for, str(path), name[:200], sha, now.isoformat(), via),
    )
    conn.commit()
    marked = _mark_stale(conn, assignment_ids) if is_new else []
    return {"file_id": r.file_id, "new": is_new, "via": via, "assignments": assignment_ids, "cards_marked": marked}


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
