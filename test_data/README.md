# Test data

A censored snapshot of one real student's Canvas account, pulled on 2026-10-29: 9 courses, 102 assignments, 4 teacher comments and 1 scored rubric. The files copy the shape of the Canvas API responses that `app/canvas.py` reads, so they can back a mock transport in tests or feed demo work without a token.

| File | Mirrors |
| --- | --- |
| `users_self.json` | `GET /api/v1/users/self` |
| `courses.json` | `GET /api/v1/courses` |
| `assignments_<course id>.json` | `GET /api/v1/courses/:id/assignments?include[]=submission` |
| `submissions_<course id>.json` | `GET /api/v1/courses/:id/students/submissions`: one object per assignment, the same shape as `submissions/self` with `submission_comments` and `rubric_assessment` |

Course 8 (a library page) has no assignments, and courses 5 and 7 have one each, which is useful for edge cases.

## What was changed

- The student is "Alex Student". Teachers are "Teacher A" to "Teacher L" everywhere they appeared: comment authors, first names, last names and titles. Classmates from a discussion-group list are "Classmate 01" to "Classmate 29".
- Every Canvas ID is renumbered (courses 1 to 9, assignments 1 to 102, users 1 and 101+). The school's Canvas host is `canvas.example.edu`. Google Doc, Drive, Classroom and Jotform IDs are placeholders, and file `verifier` tokens are removed.
- School-specific course and term names are generalised.
- Dropped entirely: the student's submitted work (`body`, attachments, URLs), avatars, emails, and internal or LTI fields. Everything kept is on a field whitelist.

## What was not changed

- Scores, due dates, assignment names and instructions, and the wording of teacher comments apart from names. These are real.
- Public article links and the names of authors and historical figures in the readings.

Names are removed from free text by a blocklist, so a name nobody knew to list could remain. Skim before sharing this folder outside the repo.

## Using it

```python
import json, re, httpx
from pathlib import Path
from app import canvas

D = Path("test_data")
load = lambda name: json.loads((D / name).read_text(encoding="utf-8"))

def handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    if p == "/api/v1/courses":
        return httpx.Response(200, json=load("courses.json"))
    if m := re.fullmatch(r"/api/v1/courses/(\d+)/assignments", p):
        return httpx.Response(200, json=load(f"assignments_{m[1]}.json"))
    if m := re.fullmatch(r"/api/v1/courses/(\d+)/assignments/(\d+)/submissions/self", p):
        for s in load(f"submissions_{m[1]}.json"):
            if s["assignment_id"] == int(m[2]):
                return httpx.Response(200, json=s)
    return httpx.Response(404)

canvas._client = lambda: httpx.Client(
    base_url="https://canvas.example.edu", transport=httpx.MockTransport(handler)
)
```
