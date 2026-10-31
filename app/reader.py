"""Document reader: turns an assignment's files into text, per page, and says which ones it couldn't open.

- read_document(path): one local file -> Doc. PDFs are read with pypdf; DOCX/PPTX are converted
  to PDF first (convert.py). A landscape scan of a two-page book spread is split down the middle,
  so each printed page gets its own text (`book_pages`) and `real_pages` counts book pages, not
  PDF pages. Google Docs exports, which pypdf reads as one word per line, get their lines rebuilt
  from where each word sits. Extracted text is cached next to the file, so rebuilding a card
  doesn't redo it.
- read_assignment(assignment): everything the description points at (Canvas files, Google files
  linked or embedded, Canvas pages, web articles), fetched in parallel and read. Each comes back
  as a Doc, readable or not, with the reason when not.
- model_files(docs): the local files to send the model for those docs, within a size budget.
- transcribe(doc) / read_scans(docs): a scan with no text layer, read by the model the first time
  a card or practice set uses it and cached by file hash, so the same file is never sent twice.

Canvas fetches are skipped without a token, so tests never make them. Google and web fetches go
through gdocs._client / _web_client, which tests replace.
"""

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app import browser_files, canvas, convert, gdocs, meta_client, web
from app.config import settings
from app.models import Assignment, SourceFile

log = logging.getLogger(__name__)
logging.getLogger("pypdf").setLevel(logging.ERROR)  # scanned PDFs log a warning per broken xref

CACHE_VERSION = 3
FETCH_WORKERS = 4
MODEL_MAX_BYTES = 20_000_000  # a PDF bigger than this goes to the model as its extracted text
MODEL_PAGE_BUDGET = 60  # PDF pages sent as PDFs per card; past this, docs go as text
MODEL_MAX_FILES = 5
PAGE_CACHE_SECONDS = 6 * 3600  # Canvas pages are fetched again after this

DocKind = Literal["file", "google", "page", "article", "video", "app"]


class BookPage(BaseModel):
    pdf_page: int  # 1-based PDF page this came from
    number: str | None = None  # printed page number, when the page shows one
    text: str = ""


class Doc(BaseModel):
    name: str
    url: str = ""  # where the student opens it
    kind: DocKind = "file"
    path: str = ""  # local file, when it was downloaded
    readable: bool = False
    reason: str = ""  # why not, when it isn't readable (or why there's no text, or where it came from)
    pdf_pages: int = 0
    pages: list[str] = Field(default_factory=list)  # text per PDF page (or one entry for a text doc)
    book_pages: list[BookPage] = Field(default_factory=list)  # spreads split into their two pages
    real_pages: int = 0

    @property
    def text(self) -> str:
        return "\n\n".join(p for p in self.pages if p.strip())

    def source(self) -> SourceFile:
        """The card's record of this doc."""
        return SourceFile(
            name=self.name, url=self.url, read=self.readable, reason=self.reason,
            pages=self.real_pages or None,
        )


# ---- reading one file -------------------------------------------------------------------------

_SPREAD_MIN_RATIO = 1.15  # landscape pages wider than this may be two book pages side by side
_SPREAD_MAX_RATIO = 1.6  # 16:9 slides are 1.78; a letter-size spread is 1.29


def _cache_file(path: Path) -> Path:
    return path.with_name(path.name + ".reader.json")


def _load_cache(path: Path) -> Doc | None:
    cache = _cache_file(path)
    if not cache.exists():
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        stat = path.stat()
        if data.get("version") != CACHE_VERSION or data.get("size") != stat.st_size or data.get("mtime") != stat.st_mtime:
            return None
        return Doc.model_validate(data["doc"])
    except (OSError, ValueError):
        return None


def _save_cache(path: Path, doc: Doc) -> None:
    try:
        stat = path.stat()
        payload = {"version": CACHE_VERSION, "size": stat.st_size, "mtime": stat.st_mtime, "doc": doc.model_dump()}
        _cache_file(path).write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        log.warning("couldn't cache text for %s: %s", path.name, e)


_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})


def _tidy(text: str) -> str:
    text = text.replace("﻿", "").replace("\r", "").translate(_LIGATURES)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _runs(page) -> list[tuple[float, float, str]]:
    """Each text run on a page with where it starts. pypdf's own line breaks are dropped: they
    arrive with a stale position (and Google Docs exports get one between every word)."""
    runs: list[tuple[float, float, str]] = []

    def visit(text, cm, tm, font_dict, font_size):
        if not text or (not text.strip() and "\n" in text):
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        runs.append((x, y, text))

    page.extract_text(visitor_text=visit)
    return runs


def _join(runs: list[tuple[float, float, str]]) -> str:
    """Runs back into lines: a new line wherever the text moves up or down the page."""
    out: list[str] = []
    last_y = None
    for _, y, text in runs:
        if last_y is not None and abs(y - last_y) > 1 and out and not out[-1].endswith("\n"):
            out.append("\n")
        last_y = y
        out.append(text)
    return _tidy("".join(out))


def _word_per_line(raw: list[str]) -> bool:
    """pypdf's text for Google Docs (and Chrome) PDFs: a blank line between every word."""
    lines = [l for text in raw for l in text.split("\n")]
    blank = sum(1 for l in lines if l and not l.strip())
    words = sum(len(l.split()) for l in lines)
    return len(lines) >= 10 and blank >= 0.4 * len(lines) and words < 3 * max(1, len(lines) - blank)


def _halves(runs: list[tuple[float, float, str]], width: float) -> tuple[str, str]:
    """Text on the left and right half of a page, split by where each text run starts."""
    return _join([r for r in runs if r[0] < width / 2]), _join([r for r in runs if r[0] >= width / 2])


_HEAD_NUMBER = [
    re.compile(r"^(\d{1,4})\s+\S"),  # "178 Chapter Seven"
    re.compile(r"\S\s+(\d{1,4})$"),  # "The Unbreakable Chain 179"
    re.compile(r"^(\d{1,4})$"),  # a page number on its own line
]


def _printed_number(text: str) -> str | None:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines[:2] + lines[-1:]:
        if len(line) > 60:
            continue
        for pattern in _HEAD_NUMBER:
            if m := pattern.search(line):
                return m.group(1)
    return None


def _check_numbers(pages: list[BookPage]) -> list[BookPage]:
    """Keep printed numbers only if they run in sequence (so poem line numbers don't count)."""
    numbered = [(i, int(p.number)) for i, p in enumerate(pages) if p.number and p.number.isdigit()]
    pairs = list(zip(numbered, numbered[1:]))
    good = sum(1 for (i, a), (j, b) in pairs if b - a == j - i)
    if len(numbered) < 2 or good * 2 < len(pairs):
        return [p.model_copy(update={"number": None}) for p in pages]
    return pages


def _read_pdf(path: Path, allow_spreads: bool = True) -> Doc:
    import pypdf  # imported here so the app starts even if the dependency is missing

    try:
        pdf = pypdf.PdfReader(str(path))
        raw = list(pdf.pages)
    except Exception as e:  # pypdf raises many types for broken files; the model may still manage
        log.warning("pypdf couldn't open %s: %s", path.name, e)
        return Doc(name=path.name, path=str(path), readable=True, reason="couldn't extract its text")
    plain: list[str] = []
    for n, page in enumerate(raw, 1):
        try:
            plain.append(page.extract_text() or "")
        except Exception as e:
            log.warning("page %s of %s unreadable: %s", n, path.name, e)
            plain.append("")
    rebuild = _word_per_line(plain)
    pages: list[str] = []
    book: list[BookPage] = []
    for n, page in enumerate(raw, 1):
        try:
            box = page.mediabox
            width, height = float(box.width), float(box.height)
            if page.get("/Rotate") in (90, 270):
                width, height = height, width
            spread = allow_spreads and height and _SPREAD_MIN_RATIO < width / height < _SPREAD_MAX_RATIO
            runs = _runs(page) if rebuild or spread else []
            text = _join(runs) if rebuild else _tidy(plain[n - 1])
            halves = None
            if spread:
                left, right = _halves(runs, width)
                if len(left) + len(right) == 0 or min(len(left), len(right)) > 0.15 * (len(left) + len(right)):
                    halves = (left, right)
        except Exception as e:
            log.warning("page %s of %s unreadable: %s", n, path.name, e)
            text, halves = "", None
        pages.append(text)
        if halves:
            book.extend(BookPage(pdf_page=n, number=_printed_number(h), text=h) for h in halves)
        else:
            book.append(BookPage(pdf_page=n, number=_printed_number(text), text=text))
    book = _check_numbers(book)
    has_text = any(p.strip() for p in pages)
    return Doc(
        name=path.name, path=str(path), readable=True, pdf_pages=len(raw), pages=pages,
        book_pages=book, real_pages=len(book),
        reason="" if has_text else "scanned without a text layer: the model can still read the images",
    )


def read_document(path: Path, name: str = "", url: str = "", kind: DocKind = "file") -> Doc:
    """Read one local file. Office files are converted to PDF first; the result is cached."""
    path = Path(path)
    if not path.exists():
        return Doc(name=name or path.name, url=url, kind=kind, reason="file missing")
    doc = _load_cache(path)
    if doc is None:
        doc = _read_uncached(path)
        if doc.readable:
            _save_cache(path, doc)
    return doc.model_copy(update={"name": name or doc.name, "url": url or doc.url, "kind": kind})


def _read_uncached(path: Path) -> Doc:
    suffix = path.suffix.lower()
    if suffix in convert.TEXT:
        text = _tidy(path.read_text(encoding="utf-8", errors="replace"))
        return Doc(name=path.name, path=str(path), readable=bool(text), reason="" if text else "empty file",
                   pages=[text], book_pages=[BookPage(pdf_page=1, text=text)], real_pages=1 if text else 0)
    if suffix in convert.IMAGES:
        return Doc(name=path.name, path=str(path), readable=True, real_pages=1,
                   reason="an image: the model reads it directly", book_pages=[BookPage(pdf_page=1)])
    if suffix in convert.OFFICE:
        pdf = convert.to_pdf(path)
        if pdf is None:
            why = "couldn't convert it (LibreOffice isn't installed)" if not convert.find_soffice() else "couldn't convert it to PDF"
            return Doc(name=path.name, reason=why)
        slides = suffix in {".ppt", ".pptx", ".odp"}
        return _read_pdf(pdf, allow_spreads=not slides).model_copy(update={"name": path.name})
    if suffix == ".pdf":
        return _read_pdf(path, allow_spreads=not path.name.startswith("google-slides-"))
    return Doc(name=path.name, reason=f"can't read {suffix or 'this kind of'} files")


def text_pages(path: Path) -> tuple[list[str], str]:
    """Text per page and why there's none (or that it came from a scan), for callers that only
    want text (materials.py). Spreads come back as their book pages. A scan is transcribed by the
    model the first time, so call this only for documents a card or practice set will use."""
    doc = read_document(path)
    if not doc.readable:
        return [], doc.reason
    doc = transcribe(doc)
    pages = [p.text for p in doc.book_pages] if doc.book_pages else doc.pages
    if not any(p.strip() for p in pages):
        return [], doc.reason
    return [p for p in pages if p.strip()], read_note(doc)


# ---- scans: text read once by the model -------------------------------------------------------

SCAN_MAX_PAGES = 12  # pages of one scan sent to be transcribed; the rest stay without text
SCAN_RETRY_SECONDS = 30 * 60  # after a failed transcription, wait this long before paying again
FROM_SCAN = "read from a scan by the model"
SCAN_INSTRUCTIONS = (
    "You transcribe scanned school documents (worksheets, answer keys, readings, tests) into plain "
    "text. Copy the words exactly as printed or handwritten, page by page: headings, question "
    "numbers, answer choices, worked solutions and line breaks. Write math in plain text "
    "(x^2, sqrt(3), 30 deg, v = d/t). For a diagram or graph, write [diagram: what it shows, with "
    "its labels and values]. Don't summarize, solve, correct or add anything."
)


class _ScanPage(BaseModel):
    pdf_page: int  # 1-based page of the file
    printed_number: str = ""  # the page number printed on the page, if it shows one
    text: str


class _Scan(BaseModel):
    pages: list[_ScanPage]


_scan_locks: dict[str, threading.Lock] = {}
_scan_locks_guard = threading.Lock()


def is_scan(doc: Doc) -> bool:
    """A file the model can see that has no text of its own: a scanned PDF or a photo."""
    return doc.readable and bool(doc.path) and not doc.text.strip() and Path(doc.path).suffix.lower() in meta_client.MIME


def read_note(doc: Doc) -> str:
    """Where a readable doc's text came from, when the student should know: a scan or a page view."""
    return doc.reason if doc.readable and doc.reason.startswith((FROM_SCAN, PAGE_VIEW)) else ""


def _scan_cache(sha: str) -> Path:
    return Path(settings.files_dir) / "scans" / f"{sha}.json"


def _scan_file(path: Path, pdf_pages: int, sha: str) -> Path:
    """The file to send: the scan itself, or a copy of its first SCAN_MAX_PAGES pages."""
    if path.suffix.lower() != ".pdf" or pdf_pages <= SCAN_MAX_PAGES:
        return path
    import pypdf

    out = _scan_cache(sha).with_suffix(".pdf")
    if not out.exists():
        writer = pypdf.PdfWriter()
        for page in pypdf.PdfReader(str(path)).pages[:SCAN_MAX_PAGES]:
            writer.add_page(page)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as f:
            writer.write(f)
    return out


def _transcribe(path: Path, pdf_pages: int, sha: str) -> list[_ScanPage]:
    sent = _scan_file(path, pdf_pages, sha)
    if sent.stat().st_size > MODEL_MAX_BYTES:
        raise meta_client.MetaError(f"{sent.stat().st_size // 1_000_000} MB is too big to send")
    n = min(pdf_pages, SCAN_MAX_PAGES) or 1
    prompt = (
        f"Transcribe this scan ({n} page{'' if n == 1 else 's'}): one entry per page, in order. If one "
        "page shows two book pages side by side, give each book page its own entry with the same pdf_page."
    )
    got = meta_client.generate_structured(prompt, _Scan, files=[sent], instructions=SCAN_INSTRUCTIONS)
    return [p for p in got.pages if p.text.strip()]


def _with_scan_text(doc: Doc, pages: list[_ScanPage]) -> Doc:
    if not pages:
        return doc
    total = max(doc.pdf_pages, 1)
    read = min(total, SCAN_MAX_PAGES)
    per_pdf = [""] * total
    book: list[BookPage] = []
    for p in pages:
        n = min(max(p.pdf_page, 1), read)
        text = _tidy(p.text)
        per_pdf[n - 1] = (per_pdf[n - 1] + "\n\n" + text).strip()
        book.append(BookPage(pdf_page=n, number=p.printed_number.strip() or _printed_number(text), text=text))
    book += [BookPage(pdf_page=n) for n in range(read + 1, total + 1)]
    reason = FROM_SCAN if read == total else f"{FROM_SCAN} (the first {read} of {total} pages)"
    update = {"pages": per_pdf, "book_pages": _check_numbers(book), "real_pages": len(book), "reason": reason}
    return doc.model_copy(update=update)


def transcribe(doc: Doc) -> Doc:
    """A scan with its text, read by the model (standard tier) the first time anything asks and
    cached by the file's hash, so the same file in two folders is sent once. Docs that have text,
    and every doc when the model isn't set up or fails, come back unchanged."""
    if not is_scan(doc) or not meta_client.configured():
        return doc
    path = Path(doc.path)
    try:
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return doc
    with _scan_locks_guard:
        lock = _scan_locks.setdefault(sha, threading.Lock())
    with lock:  # cards build on several threads: a scan two of them use is still sent once
        cache = _scan_cache(sha)
        failed = cache.with_suffix(".failed")
        try:
            pages = [_ScanPage(**p) for p in json.loads(cache.read_text(encoding="utf-8"))["pages"]]
        except (OSError, ValueError, KeyError, TypeError):
            if failed.exists() and time.time() - failed.stat().st_mtime < SCAN_RETRY_SECONDS:
                return doc  # failed a moment ago: don't pay for the same failure on every rebuild
            try:
                pages = _transcribe(path, doc.pdf_pages, sha)
            except Exception as e:  # best effort: a card never fails because a scan couldn't be read
                log.warning("couldn't transcribe the scan %s: %s", path.name, e)
                failed.parent.mkdir(parents=True, exist_ok=True)
                failed.write_text(f"{path.name}: {e}"[:500], encoding="utf-8")
                return doc
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"file": path.name, "pages": [p.model_dump() for p in pages]}), encoding="utf-8")
            failed.unlink(missing_ok=True)
            log.info("transcribed the scan %s: %d pages", path.name, len(pages))
    return _with_scan_text(doc, pages)


def read_scans(docs: list[Doc]) -> list[Doc]:
    """docs, with the scans among those model_files would send transcribed, in parallel."""
    _, sent = model_files(docs)
    todo = [d for d in sent if is_scan(d)]
    if not todo or not meta_client.configured():
        return docs
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(todo))) as pool:
        done = {id(d): t for d, t in zip(todo, pool.map(transcribe, todo))}
    return [done.get(id(d), d) for d in docs]


# ---- everything an assignment links to --------------------------------------------------------


def _folder(assignment: Assignment) -> Path:
    return Path(settings.files_dir) / "attachments" / str(assignment.id)


def _canvas_ok() -> bool:
    return bool(settings.canvas_base_url and settings.canvas_token)


_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "edpuzzle.com")
_APP_HOSTS = (
    "docs.google.com/forms", "forms.gle", "classroom.google.com", "deltamath.com", "jotform.com",
    "khanacademy.org", "quizlet.com", "kahoot", "desmos.com", "padlet.com", "flip.com",
)
_CANVAS_PAGE = re.compile(r"/courses/(\d+)/pages/([^/?#]+)")


def _classify(url: str) -> DocKind | None:
    """What kind of thing a description link is, or None for links that aren't documents
    (other Canvas assignments, the syllabus, mail links)."""
    host = urlparse(url).netloc.lower()
    if gdocs.is_google_file(url):
        return "google"
    if settings.canvas_base_url and url.startswith(settings.canvas_base_url) or host.endswith("instructure.com"):
        if "/files/" in url:
            return None  # read through the file's API endpoint instead
        return "page" if _CANVAS_PAGE.search(url) else None
    if any(h in host for h in _VIDEO_HOSTS):
        return "video"
    if any(h in url for h in _APP_HOSTS):
        return "app"
    return "article"


def _plan(assignment: Assignment) -> list[tuple[str, str, str, DocKind]]:
    """(key, label, url, kind) for each document, deduplicated, embedded docs first: a description
    that is one big iframe usually *is* the instructions."""
    _, links, files = canvas.parse_description(assignment.description_html)
    out: list[tuple[str, str, str, DocKind]] = []
    seen: set[str] = set()

    def add(key: str, label: str, url: str, kind: DocKind) -> None:
        if key not in seen:
            seen.add(key)
            out.append((key, label, url, kind))

    embedded = {l.url for l in canvas.embedded_links(assignment.description_html)}
    for link in sorted(links, key=lambda l: l.url not in embedded):
        kind = _classify(link.url)
        if kind == "google":
            add("google-" + gdocs.file_id(link.url), link.label, link.url, kind)
        elif kind is not None:
            add(link.url.split("#")[0], link.label, link.url, kind)
    for endpoint in files:
        file_id = endpoint.rstrip("/").rsplit("/", 1)[-1]
        label = next((l.label for l in links if f"/files/{file_id}" in l.url), f"Canvas file {file_id}")
        add("canvas-" + file_id, label, endpoint, "file")
    return out


def _canvas_file(endpoint: str, label: str, folder: Path) -> Doc:
    if not _canvas_ok():
        return Doc(name=label, kind="file", reason="no Canvas token")
    got = canvas.fetch_file(endpoint, folder)
    if got.path is None:
        return Doc(name=got.name or label, url=got.url, kind="file", reason=got.reason)
    return read_document(got.path, name=got.name or label, url=got.url, kind="file")


PAGE_VIEW = "read from the page view: downloading is turned off"


def _uploaded(saved: browser_files.Stored, name: str, url: str) -> Doc:
    """A file the Chrome extension uploaded, read like a downloaded one."""
    doc = read_document(saved.path, name=saved.title or name, url=url, kind="google")
    if doc.readable and saved.via == "page":
        doc = doc.model_copy(update={"reason": PAGE_VIEW})
    return doc


def _google_file(url: str, label: str, folder: Path) -> Doc:
    """A Google file: the copy the Chrome extension uploaded if there is one, else a public download."""
    saved = browser_files.stored(url)
    if saved is not None:
        return _uploaded(saved, label, url)
    got = gdocs.fetch(url, folder)
    if got.path is None:
        # the extension fetches signed in, so its reason (e.g. downloads turned off) is the truer one
        return Doc(name=got.title or label, url=url, kind="google", reason=browser_files.reason(url) or got.reason)
    return read_document(got.path, name=got.title or label, url=url, kind="google")


def _assignment_copies(assignment: Assignment) -> list[Doc]:
    """The student's Google Assignments copy, when the extension has uploaded it."""
    return [_uploaded(s, "Your copy (Google Assignments)", s.url) for s in browser_files.copies(assignment.id)]


def _canvas_page(url: str, label: str, folder: Path) -> Doc:
    if not _canvas_ok():
        return Doc(name=label, url=url, kind="page", reason="no Canvas token")
    m = _CANVAS_PAGE.search(url)
    path = folder / f"page-{m.group(1)}-{re.sub(r'[^\w-]', '_', m.group(2))}.txt"
    if not path.exists() or time.time() - path.stat().st_mtime > PAGE_CACHE_SECONDS:
        page = canvas.fetch_page(int(m.group(1)), m.group(2))
        if page is None:
            return Doc(name=label, url=url, kind="page", reason="page not found or locked")
        text, _, _ = canvas.parse_description(page.get("body") or "")
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{page.get('title') or label}\n\n{text}", encoding="utf-8")
    title = path.read_text(encoding="utf-8").split("\n", 1)[0].strip()
    return read_document(path, name=title or label, url=url, kind="page")


def _article(url: str, label: str, folder: Path, kind: DocKind) -> Doc:
    if kind == "video":
        return Doc(name=label, url=url, kind=kind, reason="a video (not read)")
    if kind == "app":
        return Doc(name=label, url=url, kind=kind, reason="an interactive site (not read)")
    got = web.fetch(url, folder)
    if got.path is None:
        return Doc(name=got.title or label, url=url, kind=kind, reason=got.reason)
    return read_document(got.path, name=got.title or label, url=url, kind=kind)


_GENERIC_LABELS = {"here", "link", "this", "click here", "this link", "pdf", "transcript", "file"}


def _label(label: str, url: str) -> str:
    """A link's text, or something better when the text is a URL or just "here"."""
    if label.strip().lower() not in _GENERIC_LABELS and not label.startswith(("http", "/")):
        return label
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[-_]+", " ", Path(tail).stem).strip() or label


def _read_one(item: tuple[str, str, str, DocKind], folder: Path) -> Doc:
    key, label, url, kind = item
    label = _label(label, url)
    try:
        if kind == "file":
            return _canvas_file(url, label, folder)
        if kind == "google":
            return _google_file(url, label, folder)
        if kind == "page":
            return _canvas_page(url, label, folder)
        return _article(url, label, folder, kind)
    except httpx.HTTPError as e:
        log.warning("reading %s failed: %s", url, e)
        return Doc(name=label, url=url, kind=kind, reason=f"download failed ({type(e).__name__})")


def read_assignment(assignment: Assignment) -> list[Doc]:
    """Every document the assignment points at, read (or not, with the reason): the student's
    Google Assignments copy first, then embedded docs, then the rest in description order."""
    copies = _assignment_copies(assignment)
    items = _plan(assignment)
    if not items:
        return copies
    folder = _folder(assignment)
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(items))) as pool:
        return copies + list(pool.map(lambda item: _read_one(item, folder), items))


def model_files(docs: list[Doc]) -> tuple[list[Path], list[Doc]]:
    """Files to send the model for `docs`, and the docs they cover. PDFs go as PDFs (the model sees
    the layout and any images) until MODEL_PAGE_BUDGET pages; after that, or when a PDF is over
    MODEL_MAX_BYTES, its extracted text goes instead. Unreadable docs are skipped."""
    files: list[Path] = []
    sent: list[Doc] = []
    pages = 0
    for doc in docs:
        if not doc.readable or not doc.path or len(files) >= MODEL_MAX_FILES:
            continue
        path = Path(doc.path)
        if path.suffix.lower() == ".pdf":
            too_big = path.stat().st_size > MODEL_MAX_BYTES or pages + doc.pdf_pages > MODEL_PAGE_BUDGET
            if too_big:
                if not doc.text.strip():
                    continue
                path = path.with_name(path.stem + ".text.txt")
                if not path.exists():
                    path.write_text(_text_with_page_marks(doc), encoding="utf-8")
            else:
                pages += doc.pdf_pages
        if path not in files:
            files.append(path)
            sent.append(doc)
    return files, sent


def _text_with_page_marks(doc: Doc) -> str:
    parts = [f"{doc.name} (extracted text, {doc.real_pages} pages)"]
    for p in doc.book_pages or [BookPage(pdf_page=i, text=t) for i, t in enumerate(doc.pages, 1)]:
        mark = f"page {p.number}" if p.number else f"PDF page {p.pdf_page}"
        parts.append(f"--- {mark} ---\n{p.text}")
    return "\n\n".join(parts)
