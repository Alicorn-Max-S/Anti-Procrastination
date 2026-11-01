"""The labeled set: every hand-labeled assignment and test question, as the decider sees it.

Assignments come from the labeling page's own item builder (the app DB plus last year's sample), so
the model reads exactly what Max labeled. Test questions were labeled on page images, so their text
comes from Spark's grader: each test is read once, one call per page with the grader's own prompt,
and cached in data/eval/tests/<test id>.json. Its rows are matched to the labeled question numbers:
in order when the counts match, else by printed number, else by the file's "question_map", which
gives each row the labeled numbers it covers (a row with several blanks labeled one by one). Such a
row takes its blanks' shared labels, and "partial" credit when some blanks got credit and some not.

A label whose value is no longer one of the field's options is skipped and listed, and so is every
label of a test whose pages aren't here or whose rows can't be matched, and every label a prompt
example was taken from (decisions/fields.py: an example must not grade itself).
"""

import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.meta_client import MetaError
from decider.prompts import assignment_context, test_context
from decisions.fields import (
    ASSIGNMENT_FIELDS,
    ASSIGNMENT_KIND,
    FIELDS_BY_KIND,
    QUESTION_FIELDS,
    TEST_QUESTION_KIND,
    Field,
    field_values,
)

ROOT = Path(__file__).resolve().parent.parent
EVAL_DATA = ROOT / "data" / "eval"
LABELS = ROOT / "data" / "labeling" / "labels.jsonl"
TESTS = ROOT / "data" / "labeling" / "tests"


@dataclass
class Item:
    kind: str  # assignment or test_question
    key: str  # "<kind>:<item id>", as in labels.jsonl
    course: str
    data: dict  # the assignment as the labeling page builds it, or one grader row
    labels: dict[str, str]  # field -> label, current options only
    topics: list[str] = field(default_factory=list)  # the class's topics (test questions)
    test: str = ""  # the test id: a test's questions are decided together

    @property
    def fields(self) -> tuple[Field, ...]:
        return FIELDS_BY_KIND[self.kind]

    def context(self) -> str:
        """What every setup sees of the item (decider/prompts.py)."""
        if self.kind == ASSIGNMENT_KIND:
            return assignment_context(self.data)
        return test_context(self.data, self.topics, self.course)


@dataclass(frozen=True)
class Skipped:
    key: str
    field: str
    label: str
    reason: str


def read_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:  # blank, or a half-written last line
            continue
    return rows


def held_out() -> set[tuple[str, str]]:
    """(item key, field) of every label a prompt example was taken from."""
    return {(e.source, f.key) for f in FIELDS_BY_KIND[ASSIGNMENT_KIND] + QUESTION_FIELDS
            for e in f.examples if e.source}


def _valid(key: str, fields: tuple[Field, ...], labels: dict[str, str], topics: list[str],
           skipped: list[Skipped]) -> dict[str, str]:
    known = {f.key for f in fields}
    examples = held_out()
    keep = {}
    for f in fields:
        if f.key not in labels:
            continue
        if (key, f.key) in examples:
            skipped.append(Skipped(key, f.key, labels[f.key], "used as a prompt example"))
            continue
        allowed = topics if f.per_class else field_values(f)
        if labels[f.key] in allowed:
            keep[f.key] = labels[f.key]
        else:
            skipped.append(Skipped(key, f.key, labels[f.key], "not a current option"))
    for name in labels.keys() - known:
        skipped.append(Skipped(key, name, labels[name], "no such field"))
    return keep


# ---- test questions: Spark reads the pages ----------------------------------------------------


def read_pages(files: list[Path], course_id: int, course_name: str) -> list:
    """Spark reads each page with the grader's prompt (app/grader.py), one call per page."""
    from app import grader, meta_client
    from app.models import Course

    course = Course(id=course_id, name=course_name)
    lists = {course_id: grader.seed_qtypes(course_name)}
    pages: list = []
    with tempfile.TemporaryDirectory() as tmp:
        parts: list[Path] = []
        for n, f in enumerate(files):  # a multi-page PDF is split the way the grader splits it
            folder = Path(tmp) / str(n)
            folder.mkdir()
            parts += grader.split_pages(f, folder)
        for n, part in enumerate(parts, 1):
            previous = pages[-1].sections[-1] if pages and pages[-1].sections else None
            prompt = grader._page_prompt(n, len(parts), [course], lists, course, [], previous)
            pages.append(meta_client.generate_structured(
                prompt, grader.GradedPage, files=[part], instructions=grader.INSTRUCTIONS))
    return pages


def _g(x: float) -> str:
    return f"{x:g}"


def written_marks(it, section) -> str:
    """Everything the teacher wrote at a question, as written: its marks ("X", "✓", "-1"), the
    points it earned ("2.5/4") or lost ("-1.5") when a number is written there, and, for a question
    alone in its section, the section's score, since that is the question's own score ("2/3")."""
    parts = [it.marks_seen.strip()]
    if it.points_earned is not None:
        parts.append(f"{_g(it.points_earned)}/{_g(it.points_possible)}" if it.points_possible
                     else _g(it.points_earned))
    elif it.points_lost:
        parts.append(f"-{_g(it.points_lost)}")
    if len(section.items) == 1 and section.subtotal_earned is not None:
        parts.append(f"{_g(section.subtotal_earned)}/{_g(section.subtotal_possible)}"
                     if section.subtotal_possible else _g(section.subtotal_earned))
    out: list[str] = []
    for part in parts:
        if part and part not in out:
            out.append(part)
    return ", ".join(out)


def page_rows(pages: list) -> list[dict]:
    """One row per question in reading order, with the marks as written (spec, Part B)."""
    rows, section = [], ""
    for page in pages:
        for s in page.sections:
            if not (s.continues_previous and section):  # a carried-on section keeps its instructions
                section = s.instructions.strip() or s.name.strip()
            for it in s.items:
                number = it.number.strip()
                text = " ".join(it.question.split())
                if number and not text.startswith(number):
                    text = f"{number}. {text}"
                points = it.points_possible
                if points is None and len(s.items) == 1:
                    points = s.subtotal_possible
                rows.append({
                    "number": number, "text": text, "answer": it.student_answer,
                    "marks": written_marks(it, s), "note": it.teacher_note.strip(),
                    "points": points, "section": section, "bonus": s.bonus,
                })
    return rows


def corrected(pages: list[dict], corrections: list[dict]) -> list[dict]:
    """Spark's pages with hand fixes for marks it misread. Each correction names the question by
    page (from 1), section name and printed number, the item fields to `set`, and `why`."""
    pages = json.loads(json.dumps(pages))
    for c in corrections:
        found = [it for s in pages[c["page"] - 1]["sections"] if s["name"] == c["section"]
                 for it in s["items"] if it["number"].strip() == c["number"]]
        if len(found) != 1:
            raise ValueError(f"correction matches {len(found)} questions: {c}")
        found[0].update(c["set"])
    return pages


def test_rows(meta: dict, folder: Path, course: str) -> list[dict]:
    """The grader's rows for one uploaded test. Spark reads the pages once; the pages are cached in
    data/eval/tests/<test id>.json and the rows are rebuilt from them, after that file's
    "corrections" (marks Spark misread, checked against the page photo). To fix a bad match by hand,
    put a "fixed_rows" list in that file (one row per labeled question, in order): it wins."""
    from app.grader import GradedPage

    cache = EVAL_DATA / "tests" / f"{meta['id']}.json"
    if cache.exists():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if "fixed_rows" in saved:
            return saved["fixed_rows"]
        pages = corrected(saved["pages"], saved.get("corrections", []))
        return page_rows([GradedPage.model_validate(p) for p in pages])
    pages = read_pages([folder / p for p in meta["pages"]], meta["course_id"], course)
    rows = page_rows(pages)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"test": meta["id"], "rows": rows, "pages": [p.model_dump() for p in pages]},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    return rows


def align(rows: list[dict], n: int) -> tuple[list[dict] | None, str]:
    """Rows for labeled questions 1..n: in order when the counts match, else by printed number."""
    if len(rows) == n:
        return rows, ""
    by_number: dict[int, list[dict]] = {}
    for r in rows:
        m = re.match(r"\d+", str(r.get("number", "")))
        if m:
            by_number.setdefault(int(m.group()), []).append(r)
    if all(len(by_number.get(k, [])) == 1 for k in range(1, n + 1)):
        return [by_number[k][0] for k in range(1, n + 1)], ""
    return None, f"Spark read {len(rows)} questions where {n} are labeled"


CREDIT_MIX = {"full", "partial", "none"}


def combine(labels: list[dict[str, str]]) -> tuple[dict[str, str], dict[str, str]]:
    """One row's labels from its blanks' labels: the value they share, or for credit "partial" when
    they differ. Returns (labels, conflicts as field -> the differing values)."""
    out, conflicts = {}, {}
    for f in dict.fromkeys(k for lab in labels for k in lab):
        vals = [lab[f] for lab in labels if f in lab]
        if len(set(vals)) == 1:
            out[f] = vals[0]
        elif f == "credit" and set(vals) <= CREDIT_MIX:
            out[f] = "partial"
        else:
            conflicts[f] = "/".join(vals)
    return out, conflicts


def question_map(test_id: str) -> list[list[int]] | None:
    cache = EVAL_DATA / "tests" / f"{test_id}.json"
    return json.loads(cache.read_text(encoding="utf-8")).get("question_map") if cache.exists() else None


# ---- the whole set ----------------------------------------------------------------------------


def load(labels: Path = LABELS, tests: Path = TESTS) -> tuple[list[Item], list[Skipped]]:
    """Assignments in the labeling page's order, then each test's questions in order."""
    from labeling.server import _app_db, build_items, class_topics, current_labels

    rows = read_rows(labels)
    current = current_labels(rows)
    conn = _app_db()
    try:
        built, courses = build_items(conn)
        topics = class_topics(rows, conn)
    finally:
        conn.close()

    items: list[Item] = []
    skipped: list[Skipped] = []
    seen = set()
    for i in built:
        if i["kind"] == ASSIGNMENT_KIND and i["key"] in current:
            seen.add(i["key"])
            keep = _valid(i["key"], ASSIGNMENT_FIELDS, current[i["key"]], [], skipped)
            if keep:
                items.append(Item(ASSIGNMENT_KIND, i["key"], i["course"], i, keep))

    questions: dict[str, dict[int, str]] = {}
    for key, labels_ in current.items():
        kind, _, item_id = key.partition(":")
        if kind == TEST_QUESTION_KIND:
            test_id, _, k = item_id.rpartition(":q")
            questions.setdefault(test_id, {})[int(k)] = key
        elif key not in seen:
            for f, v in labels_.items():
                skipped.append(Skipped(key, f, v, "item not found in the app DB or last year's sample"))

    for test_id, numbered in sorted(questions.items()):
        meta_path = tests / test_id / "meta.json"
        why = ""
        if not meta_path.exists():
            why = f"no test pages at data/labeling/tests/{test_id}"
        else:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            course = courses.get(meta["course_id"], "")
            try:
                rows_ = test_rows(meta, meta_path.parent, course)
                groups = question_map(test_id)
                if groups is None:
                    aligned, why = align(rows_, meta["questions"])
                    if why:
                        why += (f"; give each row its labeled question numbers in a question_map in "
                                f"data/eval/tests/{test_id}.json")
                    else:
                        rows_, groups = aligned, [[k] for k in range(1, len(aligned) + 1)]
                elif len(groups) != len(rows_):
                    why = f"the question_map has {len(groups)} entries for {len(rows_)} rows"
            except (MetaError, httpx.HTTPError) as e:  # listed with the others instead of stopping the run
                why = f"Spark couldn't read the pages ({e})"
        if why:
            for key in numbered.values():
                for f, v in current[key].items():
                    skipped.append(Skipped(key, f, v, why))
            continue
        class_list = topics.get(meta["course_id"], [])
        mapped = {k for g in groups for k in g}
        for k, key in sorted(numbered.items()):
            if k not in mapped:
                skipped += [Skipped(key, f, v, "not matched to a question Spark read")
                            for f, v in current[key].items()]
        for row, ks in zip(rows_, groups):
            ks = [k for k in ks if k in numbered]
            if not ks:
                continue
            key = numbered[ks[0]] if len(ks) == 1 else f"{TEST_QUESTION_KIND}:{test_id}:" + "+".join(f"q{k}" for k in ks)
            labels_, conflicts = combine([current[numbered[k]] for k in ks])
            skipped += [Skipped(key, f, v, "its blanks have different labels") for f, v in conflicts.items()]
            keep = _valid(key, QUESTION_FIELDS, labels_, class_list, skipped)
            if keep:
                items.append(Item(TEST_QUESTION_KIND, key, course, {**row, "id": f"q{ks[0]}"}, keep,
                                  class_list, test_id))
    return items, skipped
