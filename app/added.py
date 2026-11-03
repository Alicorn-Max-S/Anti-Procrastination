"""Work the student puts on the list by hand, on top of what syncs from Canvas on its own.

Nothing here happens by itself: sync never adds these (Canvas doesn't always take late work, and
Modules items are often done in class), so the student picks them on the Assignments page.

- A Canvas assignment that isn't on the list: one whose due date passed ("I forgot the history
  doc"), with no due date, or due beyond the list's two weeks (`candidates`).
- A Modules item that isn't an assignment: a page, file, link or discussion from a class they
  missed (`modules`, `add_module_item`). It's saved as an assignment of its own, with a negative
  id, whose description points at the item so the card reads it like any handout.
- Their own: a name, a class (or none), what to do, and files (`add_own`), kept under
  FILES_DIR/own/<id>/ and read by reader.py like a Canvas file.

Each gets a finish-by time (default the end of tomorrow), stored in the `added` table. When db.py
reads the assignment, that time stands in for the due date, so the planner, check-ins and cards
treat it like any other work; an added Canvas assignment keeps Canvas's own date in
`canvas_due_at` for the page to show. Removing an added Canvas assignment only takes it off the
list; removing a Modules item or the student's own deletes it.
"""

import html
import logging
import re
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path

from app import canvas, db
from app.clock import as_local
from app.config import settings
from app.convert import IMAGES, OFFICE, TEXT
from app.models import Assignment

log = logging.getLogger(__name__)

OWN_SUFFIXES = {".pdf", *OFFICE, *IMAGES, *TEXT}
MAX_OWN_FILES = 5
MAX_OWN_BYTES = 25_000_000  # per file
NOT_ADDABLE = {"SubHeader"}  # a heading inside a module, not something to do


class AddError(ValueError):
    pass


def default_finish_by(now: datetime) -> datetime:
    """The end of tomorrow, in the student's zone."""
    tomorrow = as_local(now).date() + timedelta(days=1)
    return as_local(datetime.combine(tomorrow, time(23, 59)))


def own_dir(assignment_id: int) -> Path:
    return Path(settings.files_dir) / "own" / str(assignment_id)


def _finish(finish_by: datetime | None, now: datetime) -> datetime:
    finish = as_local(finish_by) if finish_by else default_finish_by(now)
    if finish <= now:
        raise AddError("Pick a finish-by time that hasn't passed")
    return finish


# ---- what can be added ------------------------------------------------------------------------


def candidates(conn: sqlite3.Connection, now: datetime, listed: set[int], done: set[int]) -> list[dict]:
    """Canvas assignments not on the list (`listed`) and not done or turned in: past due (newest
    first), then no due date, then later than the list goes (soonest first)."""
    courses = {c.id: c.name for c in db.list_courses(conn)}
    out = []
    for a in db.list_assignments(conn):
        if a.origin != "canvas" or a.submitted or a.id in listed or a.id in done:
            continue
        due = a.canvas_due_at or a.due_at if a.added else a.due_at
        group = "past" if due and due < now else "none" if not due else "later"
        out.append(
            {
                "id": a.id,
                "name": a.name,
                "course_id": a.course_id,
                "course": courses.get(a.course_id, ""),
                "due_at": due.isoformat() if due else None,
                "group": group,
                "html_url": a.html_url,
            }
        )
    rank = {"past": 0, "none": 1, "later": 2}
    stamp = lambda c: datetime.fromisoformat(c["due_at"]).timestamp() if c["due_at"] else 0.0
    return sorted(out, key=lambda c: (rank[c["group"]], -stamp(c) if c["group"] == "past" else stamp(c), c["name"]))


def modules(conn: sqlite3.Connection, course_id: int, listed: set[int]) -> list[dict]:
    """A course's modules and their items, live from Canvas, each item saying whether it's already
    on the list (an assignment that's listed, or an item added before)."""
    refs = {v["ref"] for v in db.list_added(conn).values() if v["ref"]}
    out = []
    for m in canvas.fetch_modules(course_id):
        items = []
        for it in m.get("items") or []:
            kind = it.get("type", "")
            if kind == "Assignment":
                on_list = it.get("content_id") in listed
            else:
                on_list = f"module:{course_id}:{it.get('id')}" in refs
            items.append(
                {
                    "id": it.get("id"),
                    "title": it.get("title") or "Untitled",
                    "type": kind,
                    "indent": it.get("indent") or 0,
                    "addable": kind not in NOT_ADDABLE,
                    "on_list": on_list,
                    "html_url": it.get("html_url") or "",
                }
            )
        out.append({"id": m.get("id"), "name": m.get("name") or "Module", "items": items})
    return out


# ---- adding -----------------------------------------------------------------------------------


def add_canvas(conn: sqlite3.Connection, assignment_id: int, finish_by: datetime | None, now: datetime, ref: str = "") -> Assignment:
    """Put a Canvas assignment on the list, with the time they want it done by."""
    a = db.get_assignment(conn, assignment_id)
    if a is None or a.origin != "canvas":
        raise AddError("No such Canvas assignment")
    db.save_added(conn, a.id, _finish(finish_by, now), now, ref)
    return db.get_assignment(conn, a.id)


def _anchor(url: str, label: str, endpoint: str = "") -> str:
    attrs = f' data-api-returntype="File" data-api-endpoint="{html.escape(endpoint)}"' if endpoint else ""
    return f'<p><a href="{html.escape(url)}"{attrs}>{html.escape(label)}</a></p>'


def _item_description(course_id: int, item: dict) -> tuple[str, list[str]]:
    """The item as description HTML the card can read, and its submission types (a quiz is a test)."""
    kind, title = item.get("type", ""), item.get("title") or "Untitled"
    url = item.get("url") or ""
    base = settings.canvas_base_url
    if kind == "File":
        endpoint = url or f"{base}/api/v1/courses/{course_id}/files/{item.get('content_id')}"
        return _anchor(f"{base}/courses/{course_id}/files/{item.get('content_id')}", title, endpoint), []
    if kind == "ExternalUrl" and item.get("external_url"):
        return _anchor(item["external_url"], title), []
    if kind in ("Page", "Discussion", "Quiz") and url:
        data = canvas.fetch_json(url) or {}
        body = data.get("body") or data.get("message") or data.get("description") or ""
        types = ["online_quiz"] if kind == "Quiz" else []
        return body or _anchor(item.get("html_url") or "", title), types
    return _anchor(item.get("html_url") or item.get("external_url") or "", title), []


def add_module_item(
    conn: sqlite3.Connection, course_id: int, module_id: int, item_id: int, finish_by: datetime | None, now: datetime
) -> Assignment:
    """Put a Modules item on the list. An item that is an assignment (or a quiz that has one) adds
    that assignment; anything else becomes an item of its own."""
    item = canvas.fetch_module_item(course_id, module_id, item_id)
    if item is None:
        raise AddError("Canvas didn't return that Modules item (it may be locked)")
    kind = item.get("type", "")
    if kind in NOT_ADDABLE:
        raise AddError("That's a heading, not something to do")
    ref = f"module:{course_id}:{item_id}"
    if any(v["ref"] == ref for v in db.list_added(conn).values()):
        raise AddError("That's already on your list")
    if kind == "Assignment" and item.get("content_id"):
        aid = int(item["content_id"])
        if db.get_assignment(conn, aid) is None:
            fetched = canvas.fetch_assignment(course_id, aid)
            if fetched is None:
                raise AddError("Canvas didn't return that assignment")
            db.save_assignment(conn, fetched)
        return add_canvas(conn, aid, finish_by, now, ref)
    description, types = _item_description(course_id, item)
    if kind == "Quiz":
        quiz = canvas.fetch_json(item.get("url") or "") if item.get("url") else None
        aid = (quiz or {}).get("assignment_id")
        if aid and db.get_assignment(conn, int(aid)):
            return add_canvas(conn, int(aid), finish_by, now, ref)
    finish = _finish(finish_by, now)
    _, _, files = canvas.parse_description(description)
    a = Assignment(
        id=db.next_manual_id(conn),
        course_id=course_id,
        name=item.get("title") or "Untitled",
        description_html=description,
        due_at=finish,
        html_url=item.get("html_url") or "",
        submission_types=types,
        attachment_urls=files,
        origin="module",
    )
    a = a.model_copy(update={"kind": canvas.guess_kind(a)})
    db.save_assignment(conn, a)
    db.save_added(conn, a.id, finish, now, ref)
    return db.get_assignment(conn, a.id)


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]", "_", Path(name or "file").name).strip(" .") or "file"
    return name[:120]


def add_own(
    conn: sqlite3.Connection,
    name: str,
    course_id: int,
    instructions: str,
    files: list[tuple[str, bytes]],
    finish_by: datetime | None,
    now: datetime,
) -> Assignment:
    """The student's own work: what it is, what to do, and any files (PDF, Office, images, text)."""
    name = " ".join((name or "").split())[:200]
    if not name:
        raise AddError("Give it a name")
    if len(files) > MAX_OWN_FILES:
        raise AddError(f"Add at most {MAX_OWN_FILES} files")
    for filename, data in files:
        if Path(filename).suffix.lower() not in OWN_SUFFIXES:
            raise AddError(f"Can't read {Path(filename).suffix or 'that kind of'} files: use PDF, Word, PowerPoint, an image or text")
        if len(data) > MAX_OWN_BYTES:
            raise AddError(f"{filename} is over {MAX_OWN_BYTES // 1_000_000} MB")
    if course_id and course_id not in {c.id for c in db.list_courses(conn)}:
        raise AddError("No such class")
    finish = _finish(finish_by, now)
    aid = db.next_manual_id(conn)
    folder = own_dir(aid)
    saved = []
    for filename, data in files:
        target = folder / _safe_name(filename)
        n = 2
        while target.exists():
            target = folder / f"{Path(_safe_name(filename)).stem} ({n}){Path(filename).suffix.lower()}"
            n += 1
        folder.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        saved.append(str(target.relative_to(Path(settings.files_dir))))
    paragraphs = [p.strip() for p in (instructions or "").split("\n\n") if p.strip()]
    description = "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)
    a = Assignment(
        id=aid,
        course_id=course_id or 0,
        name=name,
        description_html=description,
        due_at=finish,
        origin="own",
        local_files=saved,
    )
    a = a.model_copy(update={"kind": canvas.guess_kind(a)})
    db.save_assignment(conn, a)
    db.save_added(conn, aid, finish, now, f"own:{aid}")
    return db.get_assignment(conn, aid)


# ---- changing and removing --------------------------------------------------------------------


def set_finish_by(conn: sqlite3.Connection, assignment_id: int, finish_by: datetime, now: datetime) -> Assignment:
    added = db.list_added(conn).get(assignment_id)
    if added is None:
        raise AddError("That wasn't added by hand")
    finish = _finish(finish_by, now)
    db.save_added(conn, assignment_id, finish, datetime.fromisoformat(added["added_at"]), added["ref"])
    a = db.get_assignment(conn, assignment_id)
    if a.origin != "canvas":  # its own row carries the date too
        stored = a.model_copy(update={"due_at": finish, "added": False, "canvas_due_at": None})
        db.save_assignment(conn, stored)
    return db.get_assignment(conn, assignment_id)


def remove(conn: sqlite3.Connection, assignment_id: int, now: datetime) -> bool:
    """Take added work off the list. A Canvas assignment stays in Canvas (and comes back on its
    own if it's due soon); its planned blocks go. A Modules item or the student's own is deleted,
    files and all. False if it wasn't added by hand."""
    a = db.get_assignment(conn, assignment_id)
    if a is None or not a.added:
        return False
    if a.origin == "canvas":
        db.delete_added(conn, assignment_id)
        for b in db.list_blocks(conn):
            if b.assignment_id == assignment_id and b.status == "planned" and b.start >= now:
                db.delete_block(conn, b.id)
        return True
    db.delete_assignment(conn, assignment_id)
    folder = own_dir(assignment_id)
    if folder.exists():
        for f in sorted(folder.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        folder.rmdir()
    return True
