"""Unit materials for test prep: what the class covered before a quiz or test, as text a practice
question can cite.

- find_unit: the Canvas module that holds the test. In a course without modules, the unit is the
  assignments due in the weeks before the test (after the previous test), plus undated ones that
  share a topic word with it.
- gather: the unit's documents as Material rows: module files and pages, assignment descriptions,
  Google Docs embedded or linked in them, and teacher feedback on earlier assignments. Google Docs
  come down as plain text (school-only ones from the Chrome extension's uploads); other files go
  through the document reader. Text is cached on disk per source, so each document is downloaded
  and read once.
- excerpts: page-sized chunks, taken in turns from each document, best topic match first, until a
  character budget is spent. Only those go to the model.

Canvas calls are skipped without a token, so tests never make them.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from app import browser_files, canvas, db, gdocs, reader
from app.config import settings
from app.models import Assignment, Link

log = logging.getLogger(__name__)

BUDGET_CHARS = 24_000  # excerpt text sent per practice set, about 6k tokens
CHUNK_CHARS = 1_500
MAX_DOCS = 20  # documents read per unit, so a huge module can't mean dozens of downloads
MODULES_TTL = timedelta(hours=1)
NEARBY_DAYS = 28
OUTLINE_CHARS = 6_000  # the unit outline cards put in a test card's prompt
SCAN_NOTE = "a scan with no text to read"

MaterialKind = Literal["file", "page", "assignment", "google", "feedback"]


class Material(BaseModel):
    key: str  # stable id and cache key, e.g. "file-105462", "gdoc-1wG4..."
    title: str  # what the student sees in Canvas
    kind: MaterialKind
    url: str = ""  # where the student opens it
    pages: list[str] = Field(default_factory=list)
    note: str = ""  # why there's no text, or how it was read ("read from a scan by the model")

    @property
    def readable(self) -> bool:
        return any(p.strip() for p in self.pages)


class Unit(BaseModel):
    name: str
    from_module: bool
    items: list[dict]  # Canvas module items: type, title, content_id, page_url, url, external_url, html_url


class Excerpt(BaseModel):
    id: str  # "S3": what the model cites
    title: str
    url: str
    page: int | None  # 1-based, for documents with more than one page
    text: str

    @property
    def label(self) -> str:
        return f"{self.title}, p. {self.page}" if self.page else self.title


def _has_canvas() -> bool:
    return bool(settings.canvas_base_url and settings.canvas_token)


# ---- topic words ------------------------------------------------------------------------------

_STOP = set(
    """a an and the of for in on to with from by at as is are be or vs your you this that it its
    test tests quiz quizzes exam exams midterm final review unit chapter ch part section module
    practice homework hw key answers answer lab notes study guide presentation intro introduction
    week day class block period due new optional corrections assignment problems problem set sets""".split()
)


def stems(text: str) -> set[str]:
    """Rough topic stems: words of 4+ letters, minus stopwords, cut to 6 letters so "vectors",
    "vector" and "relative"/"relativity" meet."""
    words = re.findall(r"[a-zà-ÿ]{4,}", text.lower())
    return {w[:6] for w in words if w not in _STOP}


# ---- finding the unit -------------------------------------------------------------------------


def fetch_modules(conn: sqlite3.Connection, course_id: int) -> list[dict]:
    """The course's modules with their items, cached for an hour. [] without a token or on error."""
    key = f"materials.modules.{course_id}"
    now = datetime.now(timezone.utc)
    if cached := db.kv_get(conn, key):
        data = json.loads(cached)
        if datetime.fromisoformat(data["at"]) > now - MODULES_TTL:
            return data["modules"]
    if not _has_canvas():
        return []
    try:
        with canvas._client() as c:
            modules = canvas._get_all(
                c, f"/api/v1/courses/{course_id}/modules", [("include[]", "items"), ("include[]", "content_details")]
            )
            for m in modules:  # Canvas leaves items out of big modules
                if "items" not in m and m.get("items_url"):
                    m["items"] = canvas._get_all(c, m["items_url"], [])
    except httpx.HTTPError as e:
        log.warning("modules for course %s failed: %s", course_id, e)
        return []
    db.kv_set(conn, key, json.dumps({"at": now.isoformat(), "modules": modules}))
    return modules


def _is_item_for(item: dict, assignment: Assignment) -> bool:
    if item.get("type") == "Assignment" and item.get("content_id") == assignment.id:
        return True
    # A Canvas quiz item carries the quiz id, not the assignment id: match those by title.
    return item.get("type") == "Quiz" and (item.get("title") or "").strip() == assignment.name.strip()


def _nearby(conn: sqlite3.Connection, assignment: Assignment) -> Unit:
    """Assignments due in the weeks before the test, after the previous test, plus undated ones
    that share a topic word with it."""
    words = stems(assignment.name + " " + canvas.parse_description(assignment.description_html, "")[0])
    others = [a for a in db.list_assignments(conn, assignment.course_id) if a.id != assignment.id]
    picked: list[Assignment] = []
    if assignment.due_at:
        start = assignment.due_at - timedelta(days=NEARBY_DAYS)
        earlier_tests = [
            a.due_at for a in others
            if a.kind == "test" and a.due_at and a.due_at < assignment.due_at - timedelta(days=1)
        ]
        if earlier_tests:
            start = max(start, max(earlier_tests))
        picked = [a for a in others if a.due_at and start < a.due_at <= assignment.due_at]
    picked += [a for a in others if a not in picked and not a.due_at and words & stems(a.name)]
    picked.sort(key=lambda a: -len(words & stems(a.name)))  # topic matches first, then by due date
    items = [
        {"type": "Assignment", "content_id": a.id, "title": a.name, "html_url": a.html_url}
        for a in picked[:MAX_DOCS]
    ]
    return Unit(name=f"Assignments before {assignment.name}", from_module=False, items=items)


def find_unit(conn: sqlite3.Connection, assignment: Assignment) -> Unit:
    """The Canvas module that contains this assignment, or nearby assignments when there isn't one."""
    for m in fetch_modules(conn, assignment.course_id):
        items = m.get("items") or []
        if any(_is_item_for(it, assignment) for it in items):
            return Unit(name=m.get("name") or "This unit", from_module=True, items=items)
    return _nearby(conn, assignment)


# ---- reading documents ------------------------------------------------------------------------

_IFRAME = re.compile(r"<iframe\b[^>]*>", re.I)
_ATTR = re.compile(r'(\w[\w-]*)\s*=\s*"([^"]*)"')


def embedded_docs(html: str) -> list[Link]:
    """Documents embedded with <iframe src=...> (Canvas's way of showing a Google Doc inline),
    labelled with the iframe's title. parse_description only sees <a href> links."""
    out: list[Link] = []
    for tag in _IFRAME.findall(html or ""):
        attrs = {k.lower(): unescape(v) for k, v in _ATTR.findall(tag)}
        src = attrs.get("src", "")
        if src.startswith("http"):
            out.append(Link(label=attrs.get("title") or src, url=src))
    return out


_GOOGLE_TEXT = [
    (r"docs\.google\.com/document/d/([\w-]+)", "https://docs.google.com/document/d/{}/export?format=txt"),
    (r"docs\.google\.com/presentation/d/([\w-]+)", "https://docs.google.com/presentation/d/{}/export/txt"),
    (r"docs\.google\.com/spreadsheets/d/([\w-]+)", "https://docs.google.com/spreadsheets/d/{}/export?format=csv"),
]


def google_key(url: str) -> str | None:
    export = gdocs.export_url(url)
    found = re.search(r"([\w-]{20,})", export or "")
    return f"gdoc-{found.group(1)}" if found else None


def _tidy(text: str) -> str:
    text = text.replace("﻿", "").replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def read_file(path: Path) -> tuple[list[str], str]:
    """Text per page of a downloaded file (via the document reader), and why not when there's none."""
    pages, reason = reader.text_pages(path)
    if not pages and reader.read_document(path).readable:  # both cached
        reason = SCAN_NOTE
    return pages, reason


def _google_pages(url: str, folder: Path) -> tuple[list[str], str]:
    """A Google Doc/Slides/Sheet as plain text, a Drive file downloaded and read, or, for files
    shared only inside the school, the copy the Chrome extension uploaded."""
    for pattern, template in _GOOGLE_TEXT:
        if found := re.search(pattern, url):
            with gdocs._client() as c:
                r = c.get(template.format(found.group(1)))
            kind = r.headers.get("content-type", "")
            if r.status_code == 200 and kind.startswith(("text/plain", "text/csv")):
                return [_tidy(r.text)], ""
            break
    else:
        if path := gdocs.download(url, folder):
            return read_file(path)
    if saved := browser_files.stored(url):
        return read_file(saved.path)
    return [], browser_files.reason(url) or "shared only inside the school"


def _cache_path(key: str) -> Path:
    return Path(settings.files_dir) / "materials" / "text" / (re.sub(r"[^\w.-]", "_", key) + ".json")


def _cached(key: str, load) -> tuple[list[str], str]:
    """Pages for a source, read once. Only text is stored, so a failed download, a school-only doc
    the extension uploads later, or a scan the reader couldn't transcribe yet is tried again."""
    path = _cache_path(key)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["pages"]:  # older entries may hold an empty scan
            return data["pages"], data.get("note", "")
    try:
        pages, note = load()
    except httpx.HTTPError as e:
        log.warning("reading %s failed: %s", key, e)
        return [], "couldn't download it"
    pages = [p for p in pages if p.strip()]
    if pages:  # with how it was read, e.g. "read from a scan by the model"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pages": pages, "note": note}), encoding="utf-8")
    return pages, note


# ---- gathering the unit -----------------------------------------------------------------------


def _files_dir(course_id: int) -> Path:
    return Path(settings.files_dir) / "materials" / "files" / str(course_id)


def _html_materials(key: str, title: str, url: str, kind: MaterialKind, html: str, course_id: int) -> list[Material]:
    """An HTML body (page or assignment description) plus the Google Docs it embeds or links.

    A body that is nothing but an embedded doc becomes that doc, under the item's own title."""
    text, links, files = canvas.parse_description(html)
    docs: dict[str, Link] = {}  # by document: an iframe and a link to the same doc are one
    for link in embedded_docs(html) + links:
        if (doc_key := google_key(link.url)) and doc_key not in docs:
            docs[doc_key] = link
    out: list[Material] = []
    if len(text) > 40:
        out.append(Material(key=key, title=title, kind=kind, url=url, pages=[text]))
    for doc_key, doc in docs.items():
        name = title if len(docs) == 1 or doc.label == title else f"{title}: {doc.label}"
        pages, note = _cached(doc_key, lambda u=doc.url: _google_pages(u, _files_dir(course_id)))
        out.append(Material(key=doc_key, title=name, kind="google", url=url or doc.url, pages=pages, note=note))
    if _has_canvas():
        for endpoint in files:
            file_key = "file-" + endpoint.rstrip("/").rsplit("/", 1)[-1]
            pages, note = _cached(file_key, lambda e=endpoint: _canvas_file_pages(e, course_id))
            out.append(Material(key=file_key, title=f"{title} (attachment)", kind="file", url=url, pages=pages, note=note))
    return out


def _canvas_file_pages(endpoint: str, course_id: int) -> tuple[list[str], str]:
    path = canvas.download_file(endpoint, _files_dir(course_id))
    if path is None:
        return [], "too big or no longer on Canvas"
    return read_file(path)


def _feedback(a: Assignment) -> Material | None:
    """Teacher comments and rubric losses on an earlier, graded assignment."""
    lines = [f"Teacher comment: {c.text.strip()}" for c in a.comments if c.text.strip()]
    for r in a.rubric:
        if r.points is not None and r.points_possible and r.points < r.points_possible:
            note = f" ({r.comment.strip()})" if r.comment.strip() else ""
            lines.append(f"Lost {r.points_possible - r.points:g} of {r.points_possible:g} on {r.criterion}{note}")
    if not lines:
        return None
    score = f"Score: {a.score:g}/{a.points_possible:g}\n" if a.score is not None and a.points_possible else ""
    return Material(
        key=f"feedback-{a.id}", title=f"Feedback on {a.name}", kind="feedback", url=a.html_url,
        pages=[score + "\n".join(lines)],
    )


def _item_materials(conn: sqlite3.Connection, course_id: int, item: dict) -> list[Material]:
    kind, title = item.get("type"), (item.get("title") or "").strip()
    url = item.get("html_url") or ""
    if kind == "File" and _has_canvas() and item.get("url"):
        key = f"file-{item.get('content_id')}"
        pages, note = _cached(key, lambda: _canvas_file_pages(item["url"], course_id))
        return [Material(key=key, title=title, kind="file", url=url, pages=pages, note=note)]
    if kind == "Page" and item.get("page_url"):
        key = f"page-{course_id}-{item['page_url']}"
        html = _page_html(key, course_id, item["page_url"])
        return _html_materials(key, title, url, "page", html, course_id)
    if kind in ("Assignment", "Quiz"):
        a = db.get_assignment(conn, item["content_id"]) if kind == "Assignment" else None
        if a is None:
            a = next((x for x in db.list_assignments(conn, course_id) if x.name.strip() == title), None)
        if a is None:
            return []
        out = _html_materials(f"assignment-{a.id}", a.name, a.html_url or url, "assignment", a.description_html, course_id)
        if fb := _feedback(a):
            out.append(fb)
        return out
    if kind == "ExternalUrl" and gdocs.is_google_file(item.get("external_url") or ""):
        return _html_materials(f"link-{item.get('id')}", title, item["external_url"], "google",
                               f'<a href="{item["external_url"]}">{title}</a>', course_id)
    return []


def _page_html(key: str, course_id: int, page_url: str) -> str:
    """A wiki page's body HTML, cached alongside the text cache."""
    path = _cache_path(key + ".html")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["html"]
    if not _has_canvas():
        return ""
    try:
        page = canvas.fetch_page(course_id, page_url)
    except httpx.HTTPError as e:
        log.warning("page %s failed: %s", page_url, e)
        return ""
    if page is None:
        return ""
    html = page.get("body") or ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"html": html}), encoding="utf-8")
    return html


def gather(conn: sqlite3.Connection, assignment: Assignment, unit: Unit) -> list[Material]:
    """Every document in the unit except the test itself, read and deduplicated, in module order."""
    out: list[Material] = []
    seen: set[str] = set()
    for item in unit.items:
        if _is_item_for(item, assignment) or item.get("type") == "SubHeader":
            continue
        if len(seen) >= MAX_DOCS:
            break
        for m in _item_materials(conn, assignment.course_id, item):
            if m.key not in seen:
                seen.add(m.key)
                out.append(m)
    return out


# ---- choosing what to send --------------------------------------------------------------------


def _chunks(material: Material) -> list[tuple[int | None, str]]:
    """(page, text) pieces of about CHUNK_CHARS, split on blank lines where possible."""
    out: list[tuple[int | None, str]] = []
    many = len(material.pages) > 1
    for n, page in enumerate(material.pages, 1):
        buf = ""
        for para in re.split(r"\n\s*\n", page):
            para = para.strip()
            while len(para) > CHUNK_CHARS:  # one huge paragraph
                if buf:
                    out.append((n if many else None, buf))
                    buf = ""
                out.append((n if many else None, para[:CHUNK_CHARS]))
                para = para[CHUNK_CHARS:]
            if buf and len(buf) + len(para) > CHUNK_CHARS:
                out.append((n if many else None, buf))
                buf = ""
            buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            out.append((n if many else None, buf))
    return out


# Which documents fill the budget first, after every document has one excerpt in: the ones that
# show how the class asks questions, then homework, then notes and slides, then labs.
_PRIORITY = [
    (r"\b(review|practice|study guide|sample|key|answers?|quiz|test|exam)\b", 0),
    (r"\b(homework|hw|problems?|worksheet|exercises?)\b", 1),
    (r"\b(labs?|project|inquiry|activity|activities)\b", 3),
]


def _priority(title: str) -> int:
    return next((rank for pattern, rank in _PRIORITY if re.search(pattern, title.lower())), 2)


def excerpts(materials: list[Material], topic: str, budget: int = BUDGET_CHARS) -> list[Excerpt]:
    """The parts of the unit worth sending: every readable document gets its best-matching chunk,
    then the rest of the budget goes to review sheets and homework before slides, and slides
    before labs, taking turns within each group. Returned in document order."""
    words = stems(topic)
    queues: list[list[tuple[int, int, int | None, str]]] = []
    for m in materials:
        scored = [(len(words & stems(text)), i, page, text) for i, (page, text) in enumerate(_chunks(m))]
        queues.append(sorted(scored, key=lambda s: (-s[0], s[1])))
    picked: list[tuple[int, int, int | None, str]] = []  # (doc, chunk, page, text)
    used = 0

    def take(group: list[int]) -> None:
        nonlocal used
        for d in group:
            if not queues[d]:
                continue
            _, i, page, text = queues[d].pop(0)
            if used + len(text) > budget:
                queues[d].clear()  # this document's next chunk doesn't fit; let the others use the rest
                continue
            picked.append((d, i, page, text))
            used += len(text)

    take(list(range(len(materials))))
    for rank in sorted({_priority(m.title) for m in materials}):
        group = [d for d, m in enumerate(materials) if _priority(m.title) == rank]
        while any(queues[d] for d in group) and used < budget:
            take(group)
    picked.sort(key=lambda p: (p[0], p[1]))
    return [
        Excerpt(id=f"S{n}", title=materials[d].title, url=materials[d].url, page=page, text=text)
        for n, (d, _, page, text) in enumerate(picked, 1)
    ]


# ---- outline for card steps -------------------------------------------------------------------


def is_answer_key(title: str) -> bool:
    """Answer keys are for checking work, never a source to work from (prep's rule)."""
    from app.prep import is_answer_key as named_like_a_key  # prep imports practice, which imports this

    return named_like_a_key(title)


class Part(BaseModel):
    label: str  # "problem 3", "p. 4", or "" for a section heading
    text: str  # the problem's first line, the slide's title, or the heading


class DocOutline(BaseModel):
    """One unit document as a list of what's in it, so a card's steps can name exact problems and slides."""

    title: str
    url: str
    kind: MaterialKind
    answer_key: bool  # check answers with it only
    readable: bool
    note: str = ""  # why it wasn't read
    pages: int = 0
    parts: list[Part] = Field(default_factory=list)


_PROBLEM = re.compile(r"^\s*(\d{1,2})[.)]\s+(\S.*)$")


def _clip(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _is_heading(line: str) -> bool:
    return 4 <= len(line) <= 60 and "__" not in line and not line.endswith((".", ",", "?", ";"))


def _problems(lines: list[str]) -> list[Part]:
    """Numbered problems, labelled so each label is unambiguous. Numbering that restarts under a
    new heading ("Problem Set #2") is a new section; numbering that restarts without one is the
    sub-questions of the problem before it ("problem 12, part 2"), until the next number resumes."""
    found: list[tuple[str, str, str]] = []  # (section, label, text)
    heading = section = ""
    last, parent, new_heading = 0, None, False
    for line in lines:
        match = _PROBLEM.match(line)
        if match is None:
            if _is_heading(line):
                heading, new_heading = line.rstrip(":"), True
            continue
        n, text = int(match.group(1)), match.group(2)
        if parent is not None:
            if n != parent + 1:
                found.append((section, f"problem {parent}, part {n}", text))
                continue
            parent = None
        if n <= last:
            if not new_heading:
                parent = last
                found.append((section, f"problem {parent}, part {n}", text))
                continue
            section = heading
        if last == 0:
            section = heading
        found.append((section, f"problem {n}", text))
        last, new_heading = n, False
    sections = len({s for s, _, _ in found}) > 1
    return [Part(label=f"{s}, {label}" if sections and s else label, text=_clip(t)) for s, label, t in found]


def _slides(pages: list[str]) -> list[Part]:
    """Each page's first line, with runs of same-titled pages ("Relative Velocity Example #19" ...
    "#29") merged into one range."""
    titles = [(n, _clip(page.strip().splitlines()[0])) for n, page in enumerate(pages, 1) if page.strip()]
    out: list[Part] = []
    run: list[tuple[int, str]] = []

    def flush() -> None:
        if len(run) == 1:
            out.append(Part(label=f"p. {run[0][0]}", text=run[0][1]))
        elif run:
            out.append(Part(label=f"pp. {run[0][0]}–{run[-1][0]}", text=f"{run[0][1]} to {run[-1][1]}"))

    for n, title in titles:
        if run and re.sub(r"[\d#]+", "", title).strip() != re.sub(r"[\d#]+", "", run[-1][1]).strip():
            flush()
            run = []
        run.append((n, title))
    flush()
    return out


def _parts(m: Material) -> list[Part]:
    """Numbered problems when the document has them, else its pages (slides), else its short
    heading-like lines."""
    lines = [line.strip() for page in m.pages for line in page.splitlines() if line.strip()]
    if sum(1 for line in lines if _PROBLEM.match(line)) >= 2:
        return _problems(lines)
    if len(m.pages) > 1:
        return _slides(m.pages)
    return [Part(label="", text=_clip(h)) for h in [l for l in lines if _is_heading(l)][:12]]


def unit_outline(conn: sqlite3.Connection, assignment: Assignment) -> tuple[Unit, list[DocOutline]]:
    """The unit this assignment belongs to and an outline of each of its documents, in module order.
    No model calls; Canvas and Google reads are cached, so add_practice reuses them."""
    unit = find_unit(conn, assignment)
    out = [
        DocOutline(
            title=m.title, url=m.url, kind=m.kind, answer_key=is_answer_key(m.title), readable=m.readable,
            note=m.note, pages=len(m.pages), parts=_parts(m),
        )
        for m in gather(conn, assignment, unit) if m.kind != "feedback"
    ]
    return unit, out


def outline_text(unit: Unit, docs: list[DocOutline], max_chars: int = OUTLINE_CHARS) -> str:
    """The outline as prompt text, in module order. Every document gets its title line; the parts
    go to review sheets and homework first, then slides, then labs, until max_chars is used up,
    and a cut document says how many parts it left out."""
    where = "Canvas module" if unit.from_module else "assignments due before this one"
    heads = []
    for d in docs:
        tags = []
        if d.answer_key:
            tags.append("answer key: for checking answers only")
        if not d.readable:
            tags.append(f"not read: {d.note or 'no text'}")
        elif d.pages > 1:
            tags.append(f"{d.pages} pages")
        heads.append(f"- {d.title}" + (f" ({'; '.join(tags)})" if tags else ""))
    parts = [[f"    {p.label}: {p.text}" if p.label else f"    {p.text}" for p in d.parts] for d in docs]
    used = len(unit.name) + len(where) + 20 + sum(len(h) + 1 for h in heads)
    keep = [0] * len(docs)
    for rank in sorted({_priority(d.title) for d in docs}):
        open_ = [i for i, d in enumerate(docs) if _priority(d.title) == rank]
        while open_:  # documents of the same rank take turns
            for i in list(open_):
                line = parts[i][keep[i]] if keep[i] < len(parts[i]) else None
                if line is None or used + len(line) + 1 > max_chars:
                    open_.remove(i)
                    continue
                keep[i] += 1
                used += len(line) + 1
    lines = [f"Unit: {unit.name} ({where})"]
    for head, d_parts, n in zip(heads, parts, keep):
        lines.append(head)
        lines.extend(d_parts[:n])
        if n < len(d_parts):
            lines.append(f"    … {len(d_parts) - n} more")
    return "\n".join(lines)


def outline(conn: sqlite3.Connection, assignment: Assignment) -> str:
    """The unit outline as prompt text for a card, or "" when no document in the unit could be read."""
    try:
        unit, docs = unit_outline(conn, assignment)
    except Exception as e:  # the outline only improves a card; never fail one over it
        log.warning("unit outline for %s failed: %s", assignment.id, e)
        return ""
    return outline_text(unit, docs) if any(d.readable for d in docs) else ""
