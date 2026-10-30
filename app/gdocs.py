"""Fetches Google Docs/Slides/Sheets/Drive files linked from an assignment so the model can read them.

Works without a Google sign-in, so only for files shared "anyone with the link". Files shared
only inside the school's Google domain come back as a sign-in page (or a 401); those are
reported as unreadable so the card can point at the link instead of guessing what's in it.
"""

import re
from pathlib import Path

import httpx

MAX_BYTES = 25_000_000

# (pattern for the file id, export URL template). Docs/Slides/Sheets export to PDF; a Drive
# file downloads in its own format.
_EXPORTS = [
    (r"docs\.google\.com/document/d/([\w-]+)", "https://docs.google.com/document/d/{}/export?format=pdf"),
    (r"docs\.google\.com/presentation/d/([\w-]+)", "https://docs.google.com/presentation/d/{}/export/pdf"),
    (r"docs\.google\.com/spreadsheets/d/([\w-]+)", "https://docs.google.com/spreadsheets/d/{}/export?format=pdf"),
    (r"drive\.google\.com/file/d/([\w-]+)", "https://drive.google.com/uc?export=download&id={}"),
    (r"drive\.google\.com/open\?id=([\w-]+)", "https://drive.google.com/uc?export=download&id={}"),
]

_SUFFIX = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
}


def is_google_file(url: str) -> bool:
    return export_url(url) is not None


def export_url(url: str) -> str | None:
    for pattern, template in _EXPORTS:
        if m := re.search(pattern, url):
            return template.format(m.group(1))
    return None


def _client() -> httpx.Client:
    """One place to build the HTTP client, so tests can swap in a mock transport."""
    return httpx.Client(follow_redirects=True, timeout=30)


def download(url: str, dest_dir: Path) -> Path | None:
    """Save a publicly shared Google file into dest_dir. None if it's private, too big, or not a
    format the model can use (after convert.py)."""
    export = export_url(url)
    if export is None:
        return None
    file_id = re.search(r"([\w-]{20,})", export)
    with _client() as c:
        r = c.get(export)
    content_type = r.headers.get("content-type", "").split(";")[0].strip()
    suffix = _SUFFIX.get(content_type)
    if r.status_code != 200 or suffix is None or len(r.content) > MAX_BYTES:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"google-{file_id.group(1) if file_id else 'file'}{suffix}"
    path.write_bytes(r.content)
    return path
