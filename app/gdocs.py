"""Fetches Google Docs/Slides/Sheets/Drive files linked from an assignment so the model can read them.

Works without a Google sign-in, so only for files shared "anyone with the link". Files shared
only inside the school's Google domain come back as a sign-in page (or a 401); those are
reported as unreadable so the card can point at the link instead of guessing what's in it.
Drive downloads often come back as `application/octet-stream`, so the format is sniffed from the
file name Google sends or the first bytes.
"""

import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from pydantic import BaseModel

MAX_BYTES = 100_000_000
CACHE_SECONDS = 6 * 3600  # re-download a doc after this, in case the teacher edited it

# (pattern for the file id, export URL template, saved-file prefix). Docs/Slides/Sheets export to
# PDF; a Drive file downloads in its own format.
_EXPORTS = [
    (r"docs\.google\.com/document/(?:u/\d+/)?d/([\w-]+)", "https://docs.google.com/document/d/{}/export?format=pdf", "google"),
    (r"docs\.google\.com/presentation/(?:u/\d+/)?d/([\w-]+)", "https://docs.google.com/presentation/d/{}/export/pdf", "google-slides"),
    (r"docs\.google\.com/spreadsheets/(?:u/\d+/)?d/([\w-]+)", "https://docs.google.com/spreadsheets/d/{}/export?format=pdf", "google"),
    (r"drive\.google\.com/file/(?:u/\d+/)?d/([\w-]+)", "https://drive.google.com/uc?export=download&id={}", "google"),
    (r"drive\.google\.com/open\?id=([\w-]+)", "https://drive.google.com/uc?export=download&id={}", "google"),
]

_SUFFIX = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
    "text/plain": ".txt",
}
_MAGIC = [(b"%PDF", ".pdf"), (b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"RIFF", ".webp")]
_KNOWN = set(_SUFFIX.values()) | {".pptx", ".xlsx", ".ppt", ".xls", ".odt", ".odp", ".rtf", ".txt"}
_titles_lock = threading.Lock()  # read_assignment fetches several docs into one folder at once


class Fetched(BaseModel):
    path: Path | None = None
    title: str = ""  # the doc's own name, from the download's file name
    reason: str = ""  # why not, when path is None


def _match(url: str) -> tuple[str, str, str] | None:
    for pattern, template, prefix in _EXPORTS:
        if m := re.search(pattern, url):
            return m.group(1), template.format(m.group(1)), prefix
    return None


def is_google_file(url: str) -> bool:
    return _match(url) is not None


def export_url(url: str) -> str | None:
    found = _match(url)
    return found[1] if found else None


def file_id(url: str) -> str:
    found = _match(url)
    return found[0] if found else ""


def _client() -> httpx.Client:
    """One place to build the HTTP client, so tests can swap in a mock transport."""
    return httpx.Client(follow_redirects=True, timeout=30)


def _filename(r: httpx.Response) -> str:
    cd = r.headers.get("content-disposition", "")
    if m := re.search(r"filename\*=UTF-8''([^;]+)", cd):
        return unquote(m.group(1)).strip('"')
    if m := re.search(r'filename="([^"]+)"', cd):
        return m.group(1)
    return ""


def _suffix(r: httpx.Response, filename: str) -> str | None:
    content_type = r.headers.get("content-type", "").split(";")[0].strip()
    if content_type in _SUFFIX:
        return _SUFFIX[content_type]
    named = Path(filename).suffix.lower()
    if named in _KNOWN:
        return named
    head = r.content[:8]
    return next((s for magic, s in _MAGIC if head.startswith(magic)), None)


def fetch(url: str, dest_dir: Path) -> Fetched:
    """Save a publicly shared Google file into dest_dir (reusing a recent download), or say why not."""
    found = _match(url)
    if found is None:
        return Fetched(reason="not a Google file")
    fid, export, prefix = found
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.glob(f"{prefix}-{fid}.*"):
        if old.name == f"{prefix}-{fid}{old.suffix}" and old.suffix in _KNOWN and time.time() - old.stat().st_mtime < CACHE_SECONDS:
            title = _titles(dest_dir).get(fid, "")
            return Fetched(path=old, title=title)
    with _client() as c:
        r = c.get(export)
    host = urlparse(str(r.url)).netloc
    if r.status_code in (401, 403) or "accounts.google.com" in host or "ServiceLogin" in str(r.url):
        return Fetched(reason="shared only inside the school")
    if r.status_code == 404:
        return Fetched(reason="deleted or the link is wrong")
    if r.status_code != 200:
        return Fetched(reason=f"Google answered HTTP {r.status_code}")
    filename = _filename(r)
    suffix = _suffix(r, filename)
    if suffix is None:
        if "text/html" in r.headers.get("content-type", ""):
            return Fetched(reason="shared only inside the school")
        return Fetched(reason="not a format the model can read")
    if len(r.content) > MAX_BYTES:
        return Fetched(reason=f"too big ({len(r.content) / 1e6:.0f} MB)")
    path = dest_dir / f"{prefix}-{fid}{suffix}"
    path.write_bytes(r.content)
    title = Path(filename).stem if filename else ""
    if title:
        _save_title(dest_dir, fid, title)
    return Fetched(path=path, title=title)


def _titles(dest_dir: Path) -> dict[str, str]:
    f = dest_dir / "google-titles.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except ValueError:
        return {}


def _save_title(dest_dir: Path, fid: str, title: str) -> None:
    with _titles_lock:
        titles = _titles(dest_dir)
        titles[fid] = title
        (dest_dir / "google-titles.json").write_text(json.dumps(titles), encoding="utf-8")


def download(url: str, dest_dir: Path) -> Path | None:
    """Save a publicly shared Google file into dest_dir. None if it's private, too big, or not a
    format the model can use (after convert.py)."""
    return fetch(url, dest_dir).path
