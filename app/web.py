"""Fetches web articles linked from an assignment (news, podcast transcripts, essays) as plain text.

No readability library: the page's <article> (or <main>, or the whole body) minus scripts, menus
and footers, kept as paragraphs. A page with almost no paragraph text is a video, an app or a
paywall stub, and is reported as unreadable rather than sent to the model. Linked PDFs are saved
as PDFs. Paywalled sites (the NYT answers 403) can't be read without the student's login.
"""

import hashlib
import re
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import httpx
from pydantic import BaseModel

MAX_BYTES = 20_000_000
MIN_WORDS = 150  # less paragraph text than this isn't an article
CACHE_SECONDS = 24 * 3600
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "button", "figure"}
_BLOCK = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "pre", "br", "div", "section", "tr"}


class Fetched(BaseModel):
    path: Path | None = None
    title: str = ""
    reason: str = ""


def _client() -> httpx.Client:
    """One place to build the HTTP client, so tests can swap in a mock transport."""
    return httpx.Client(follow_redirects=True, timeout=20, headers={"User-Agent": _UA})


class _Article(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.chunks: dict[str, list[str]] = {"article": [], "main": [], "body": []}
        self._stack: list[str] = []
        self._skip = 0
        self._in_title = False
        self._open: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.chunks:
            self._open.add(tag)
        if tag in _BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
        elif not self._skip:
            self._emit(data)

    def _emit(self, text: str) -> None:
        for name in self._open:
            self.chunks[name].append(text)


def article_text(html: str) -> tuple[str, str]:
    """(title, readable text) of an HTML page."""
    parser = _Article()
    parser.feed(html)
    body = next(("".join(c) for k in ("article", "main", "body") if len("".join(c := parser.chunks[k]).split()) >= MIN_WORDS), "".join(parser.chunks["body"]))
    lines = [re.sub(r"\s+", " ", unescape(l)).strip() for l in body.splitlines()]
    lines = [l for l in lines if len(l.split()) >= 4 or (l and l[-1] in ".?!:\"")]  # drop menu crumbs
    title = re.sub(r"\s+", " ", "".join(parser.title)).strip()
    return title, "\n\n".join(lines)


def fetch(url: str, dest_dir: Path) -> Fetched:
    """Save the article's text (or the linked PDF) into dest_dir, or say why not."""
    key = hashlib.sha1(url.encode()).hexdigest()[:12]
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in (dest_dir / f"web-{key}.txt", dest_dir / f"web-{key}.pdf"):
        if old.exists() and time.time() - old.stat().st_mtime < CACHE_SECONDS:
            title = old.read_text(encoding="utf-8").split("\n", 1)[0] if old.suffix == ".txt" else ""
            return Fetched(path=old, title=title)
    with _client() as c:
        r = c.get(url)
    if r.status_code in (401, 402, 403):
        return Fetched(reason="paywalled or blocked: open it in your browser")
    if r.status_code != 200:
        return Fetched(reason=f"the site answered HTTP {r.status_code}")
    if len(r.content) > MAX_BYTES:
        return Fetched(reason="too big")
    content_type = r.headers.get("content-type", "").split(";")[0].strip()
    if content_type == "application/pdf" or r.content[:4] == b"%PDF":
        path = dest_dir / f"web-{key}.pdf"
        path.write_bytes(r.content)
        return Fetched(path=path)
    if "html" not in content_type:
        return Fetched(reason=f"not an article ({content_type or 'unknown type'})")
    title, text = article_text(r.text)
    if len(text.split()) < MIN_WORDS:
        return Fetched(title=title, reason="no article text on the page (a video, app or sign-in page)")
    path = dest_dir / f"web-{key}.txt"
    path.write_text(f"{title or url}\n\n{text}", encoding="utf-8")
    return Fetched(path=path, title=title)
