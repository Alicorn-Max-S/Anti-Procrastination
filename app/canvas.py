"""Canvas API client: pulls courses, assignments, submission comments and rubric scores.

Auth is `Authorization: Bearer <token>` against settings.canvas_base_url. Canvas paginates with a
`Link` header (rel="next"); `_get_all` follows it until it's gone.

Endpoints (from the build plan):
  GET /api/v1/users/self                      -> token check
  GET /api/v1/courses?enrollment_state=active
  GET /api/v1/courses/:id/assignments?include[]=submission
  GET /api/v1/courses/:id/assignments/:id/submissions/self
        ?include[]=submission_comments&include[]=rubric_assessment
  GET /api/v1/courses/:id/files/:id           -> download URL for a file linked in a description

Run `python -m app.canvas` to check your token. Never commit raw responses (public repo).
"""

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel

from app.config import settings
from app.models import Assignment, AssignmentKind, Course, Link, RubricScore, SubmissionComment

MAX_ATTACHMENT_BYTES = 100_000_000  # read locally; reader.model_files decides what the model gets


def _client() -> httpx.Client:
    """One place to build the HTTP client, so tests can swap in a mock transport."""
    return httpx.Client(
        base_url=settings.canvas_base_url,
        headers={"Authorization": f"Bearer {settings.canvas_token}"},
        timeout=30,
        follow_redirects=True,
    )


def _get_all(client: httpx.Client, path: str, params: list[tuple[str, str | int]]) -> list[dict]:
    """GET a paginated list endpoint and return every page joined together."""
    out: list[dict] = []
    url: str | None = path
    query: list | None = [*params, ("per_page", 100)]
    while url:
        r = client.get(url, params=query)
        r.raise_for_status()
        out.extend(r.json())
        url = r.links.get("next", {}).get("url")
        query = None  # the next link already carries the query string
    return out


def check_token() -> dict:
    """Call /users/self and return the JSON. Raises on a 401 so a bad token is obvious."""
    with _client() as c:
        r = c.get("/api/v1/users/self")
        r.raise_for_status()
        return r.json()


def fetch_courses() -> list[Course]:
    """Active courses for the signed-in student."""
    with _client() as c:
        raw = _get_all(c, "/api/v1/courses", [("enrollment_state", "active")])
    return [
        Course(id=course["id"], name=course["name"])
        for course in raw
        if "access_restricted_by_date" not in course and course.get("name")
    ]


def fetch_assignments(course_id: int) -> list[Assignment]:
    """All assignments in a course, with score, due date and rubric criteria filled in."""
    with _client() as c:
        raw = _get_all(c, f"/api/v1/courses/{course_id}/assignments", [("include[]", "submission")])
    return [assignment_from_json(course_id, a) for a in raw]


def assignment_from_json(course_id: int, a: dict) -> Assignment:
    """Map Canvas's assignment JSON onto our Assignment.

    Canvas's own `rubric` holds criteria definitions; we turn each into a RubricScore with
    `points=None`, and fetch_feedback fills in the student's points and the teacher's comment.
    """
    sub = a.get("submission") or {}
    html = a.get("description") or ""
    _, _, files = parse_description(html)
    assignment = Assignment(
        id=a["id"],
        course_id=course_id,
        name=a["name"],
        description_html=html,
        due_at=a.get("due_at"),
        points_possible=a.get("points_possible"),
        html_url=a.get("html_url") or "",
        submission_types=a.get("submission_types") or [],
        external_tool_url=(a.get("external_tool_tag_attributes") or {}).get("url") or "",
        attachment_urls=files,
        submitted=sub.get("workflow_state") in {"submitted", "graded", "pending_review"},
        score=sub.get("score"),
        graded_at=sub.get("graded_at"),
        rubric=[
            RubricScore(
                assignment_id=a["id"],
                criterion_id=str(c.get("id", "")),
                criterion=c.get("description") or "",
                points_possible=c.get("points"),
            )
            for c in a.get("rubric") or []
        ],
    )
    return assignment.model_copy(update={"kind": guess_kind(assignment)})


def fetch_feedback(course_id: int, assignment: Assignment) -> Assignment:
    """Return the assignment with `comments` and `rubric` populated from submissions/self."""
    with _client() as c:
        r = c.get(
            f"/api/v1/courses/{course_id}/assignments/{assignment.id}/submissions/self",
            params=[("include[]", "submission_comments"), ("include[]", "rubric_assessment")],
        )
        r.raise_for_status()
        sub = r.json()
    comments = [
        SubmissionComment(
            id=c["id"],
            assignment_id=assignment.id,
            author=c.get("author_name") or "",
            text=c.get("comment") or "",
            created_at=c.get("created_at"),
        )
        for c in sub.get("submission_comments") or []
        if (c.get("comment") or "").strip()
    ]
    assessment: dict = sub.get("rubric_assessment") or {}
    rubric = []
    for row in assignment.rubric:
        got = assessment.pop(row.criterion_id, None) or {}
        rubric.append(
            row.model_copy(update={"points": got.get("points"), "comment": got.get("comments") or ""})
        )
    for crit_id, got in assessment.items():  # scored criteria missing from the definition list
        rubric.append(
            RubricScore(
                assignment_id=assignment.id,
                criterion_id=crit_id,
                criterion=crit_id,
                points=got.get("points"),
                comment=got.get("comments") or "",
            )
        )
    return assignment.model_copy(
        update={"comments": comments, "rubric": rubric, "score": sub.get("score", assignment.score)}
    )


_KIND_PATTERNS: list[tuple[AssignmentKind, str]] = [
    ("test", r"\b(quiz|quizzes|test|exam|midterm|final exam|assessment)\b"),
    ("lab", r"\b(lab|labs|laboratory|experiment)\b"),
    ("essay", r"\b(essay|paper|draft|thesis|dbq|argument|narrative)\b"),
    ("reading", r"\b(read|reading|response|article|chapter|annotat\w*|novel)\b"),
    ("problem_set", r"\b(problem set|pset|problems|worksheet|homework|hw|exercises|practice)\b"),
]


def guess_kind(assignment: Assignment) -> AssignmentKind:
    """Rough AssignmentKind from the name and submission type. Deliberately dumb."""
    if "online_quiz" in assignment.submission_types:
        return "test"
    name = assignment.name.lower()
    for kind, pattern in _KIND_PATTERNS:
        if re.search(pattern, name):
            return kind
    return "other"


# ---- description HTML -------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table"}


class _DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.iframes: list[tuple[str, str]] = []
        self.files: list[str] = []
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "iframe" and a.get("src"):  # an embedded Google Doc is often the whole handout
            self.iframes.append(((a.get("title") or "").strip() or "Embedded document", a["src"]))
        if tag == "a":
            if a.get("data-api-returntype") == "File" and a.get("data-api-endpoint"):
                self.files.append(a["data-api-endpoint"])
            self._href = a.get("href")
            self._label = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.links.append(("".join(self._label).strip(), self._href))
            self._href = None
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        if self._href is not None:
            self._label.append(data)


def parse_description(html: str, base_url: str | None = None) -> tuple[str, list[Link], list[str]]:
    """Split description HTML into (plain text, links, Canvas file API endpoints). Links include
    embedded iframes (labelled with the iframe's title), after the ordinary links."""
    parser = _DescriptionParser()
    parser.feed(html or "")
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    base = base_url if base_url is not None else settings.canvas_base_url
    links: list[Link] = []
    seen: set[str] = set()
    for label, href in parser.links + parser.iframes:
        if href.startswith(("mailto:", "#", "javascript:")):
            continue
        url = urljoin(base + "/", href) if base else href
        if url in seen or not url.startswith("http"):
            continue
        seen.add(url)
        links.append(Link(label=label or url, url=url))
    files = list(dict.fromkeys(urljoin(base + "/", f) if base else f for f in parser.files))
    return text, links, files


def embedded_links(html: str) -> list[Link]:
    """Just the iframes in a description, as links (src, title)."""
    parser = _DescriptionParser()
    parser.feed(html or "")
    return [Link(label=label, url=src) for label, src in parser.iframes if src.startswith("http")]


class FetchedFile(BaseModel):
    path: Path | None = None  # None when it couldn't be downloaded
    name: str = ""  # the file's name in Canvas
    url: str = ""  # where the student opens it
    reason: str = ""  # why not, when path is None


def fetch_file(api_endpoint: str, dest_dir: Path, client: httpx.Client | None = None) -> FetchedFile:
    """Download a Canvas file (given its API endpoint) into dest_dir, reusing an earlier download
    of the same size. Says why when it can't."""
    own = client is None
    c = client or _client()
    try:
        meta = c.get(api_endpoint)
        if meta.status_code in (401, 403):
            return FetchedFile(reason="locked or not shared with students")
        if meta.status_code != 200:
            return FetchedFile(reason=f"no longer on Canvas (HTTP {meta.status_code})")
        info = meta.json()
        name = info.get("display_name") or f"file-{info.get('id')}"
        page_url = api_endpoint.replace("/api/v1/", "/", 1)
        size = info.get("size") or 0
        if info.get("locked_for_user") or not info.get("url"):
            return FetchedFile(name=name, url=page_url, reason="locked until the teacher releases it")
        if size > MAX_ATTACHMENT_BYTES:
            return FetchedFile(name=name, url=page_url, reason=f"too big to download ({size / 1e6:.0f} MB)")
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / re.sub(r"[^\w.\- ]", "_", name)
        if not path.exists() or (size and path.stat().st_size != size):
            r = c.get(info["url"])
            if r.status_code != 200:
                return FetchedFile(name=name, url=page_url, reason=f"download failed (HTTP {r.status_code})")
            path.write_bytes(r.content)
        return FetchedFile(path=path, name=name, url=page_url)
    finally:
        if own:
            c.close()


def download_file(api_endpoint: str, dest_dir: Path) -> Path | None:
    """Download a Canvas file (given its API endpoint) into dest_dir. None if it's too big or gone."""
    return fetch_file(api_endpoint, dest_dir).path


def fetch_page(course_id: int, slug: str) -> dict | None:
    """A Canvas page (title, body HTML), or None if it's missing or locked."""
    with _client() as c:
        r = c.get(f"/api/v1/courses/{course_id}/pages/{slug}")
    return r.json() if r.status_code == 200 else None


if __name__ == "__main__":
    me = check_token()
    print(f"Token OK: signed in as {me.get('name')}")
    for course in fetch_courses():
        assignments = fetch_assignments(course.id)
        open_ = sum(1 for a in assignments if not a.submitted)
        print(f"  {course.name}: {len(assignments)} assignments, {open_} not submitted")
