"""Dry run (stretch): a model attempts an assignment so the card can warn about the hard parts.

Muse Wonder (or Muse Spark, which takes the same requests) works through the assignment in its
private reasoning and, through two tools, logs every time it has to look something up, gets stuck,
backtracks or hits an ambiguous instruction. It then returns traps (each with a concrete first
step), an effort level with reasons, and what completing the assignment looks like in practice:
phases, what each one produces, and a done checklist. The solution stays in the model's reasoning,
which the API never returns as text, so there is nothing to show or store.

Design choices:
  - The model sees only what the student is given (instructions, rubric criteria, links, attachment
    text), never grades or feedback, so evaluate.py's keep-or-cut check is a fair test. Ranking by
    the student's history happens afterwards, in ranked_traps().
  - Text only. PDFs become text with pypdf (`pip install -e ".[stretch]"`); images and scanned
    pages are listed as unread instead of sent.
  - Each tool turn resends the whole input (store=false) with the encrypted reasoning replayed, so
    the model keeps its place. On a cache hit the repeated prefix bills at the cached-input rate.
  - Requests stream, since long non-streaming generations can time out with HTTP 504.
  - Standard tier only: contributor model ids are refused.

Nothing in app/ imports this. Run `python -m stretch.dry_run --help`.
"""

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from app import canvas, convert, db, meta_client, prep, student_model
from app.clock import clock
from app.config import settings
from app.meta_client import MetaError, Tool
from app.models import Assignment, Effort, PrepCard

log = logging.getLogger(__name__)

DEFAULT_MODEL = "muse-wonder-1.0"
DEFAULT_EFFORT = "high"
DEFAULT_BUDGET = 1.00  # dollars per run; the loop stops before another turn once it's spent
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
MAX_OUTPUT_TOKENS = 32_000  # per turn, reasoning included
MAX_TURNS = 12
MAX_TRAPS = 5
ATTACHMENT_CHARS = 60_000  # per file
LOG_CHARS = 300  # cap on each logged tool argument

# Dollars per million tokens: (input, cached input, output), from
# https://dev.meta.ai/docs/pricing-rate-limits on 2026-10-30. Reasoning tokens bill as output.
# A model missing here is priced as Wonder, the dearest, so the budget check errs on the safe side.
PRICES = {
    "muse-wonder-1.0": (4.00, 0.20, 20.00),
    "muse-spark-1.3": (1.25, 0.15, 4.25),
    "muse-spark-1.2": (1.25, 0.15, 4.25),
    "muse-spark-1.1": (1.25, 0.15, 4.25),
}

INSTRUCTIONS = """You are doing a dry run of one high-school assignment: attempt it the way a capable
student would, so the app can warn the real student about the hard parts before they start. Do the
attempt in your private reasoning. Nothing you write in tool calls or in the report may contain
answers, worked solutions, final values, thesis statements or drafted text for the assignment.

While you work:
- Call look_up whenever you need something a student wouldn't have in front of them: a formula, a
  definition, a fact, a source, the text of a reading you weren't given, or the teacher's expected
  format. Then carry on with what a well-prepared student would know.
- Call log_trouble the moment you get stuck, have to backtrack and redo something, or find an
  instruction ambiguous. Say where (problem number, section or step) and what happened.
- Work only from the materials given. If a part can't be done here (it needs lab equipment, class
  time, a file you can't read, or a choice only the student can make), list it under blocked.

Then return the report:
- task: one sentence on what has to be turned in.
- phases: what completing it looks like in practice, in order. For each: what you do, what exists
  when it's done, what you need, and hardest=true on the one or two that took the most effort.
- done_looks_like: a checklist of what the finished submission contains, from the instructions and
  rubric.
- traps: at most 5 places a student is most likely to lose time or points, most important first.
  kind is stuck, backtrack, lookup or ambiguous. quote: the exact words from the materials the trap
  is about, copied character for character, or "" if there are none. what_happens: what goes wrong,
  without the answer. first_step: one concrete action that gets the student past it, naming the
  problem, section or source. rubric_criterion: the exact name of the rubric criterion it puts at
  risk, or "". log_refs: the numbers of the look_up and log_trouble entries it came from.
- effort: light (under 45 minutes), medium, or heavy (over 2 hours or more than one sitting).
  effort_reasons name the specific parts that drive it. No minute estimates.
- blocked: the parts you couldn't attempt here, and why."""

LOOKUP_NOTE = (
    "Logged: a student would have to look this up. Carry on with what a well-prepared student "
    "would know, and don't log the same thing twice."
)

TrapKind = Literal["stuck", "backtrack", "lookup", "ambiguous"]


# ---- shapes -----------------------------------------------------------------------------------


class Phase(BaseModel):
    name: str
    what_you_do: str
    produces: str  # what exists when the phase is done
    needs: list[str]  # materials, sources, tools or prior knowledge
    hardest: bool


class TrapDraft(BaseModel):
    kind: TrapKind
    where: str  # problem number, section or step
    quote: str  # exact words from the materials, or ""
    what_happens: str
    first_step: str
    rubric_criterion: str  # exact criterion name, or ""
    log_refs: list[int]


class ReportDraft(BaseModel):
    """What the model returns; the checks, log and usage are added by code."""

    task: str
    phases: list[Phase]
    done_looks_like: list[str]
    traps: list[TrapDraft]
    effort: Effort
    effort_reasons: list[str]
    blocked: list[str]


class Trap(TrapDraft):
    quote_found: bool = False  # the quote really is in the materials
    observed: bool = False  # it points at a log entry made during the attempt, not only in hindsight


class LogEntry(BaseModel):
    n: int
    kind: str  # lookup, stuck, backtrack or ambiguous
    where: str
    detail: str


class Usage(BaseModel):
    turns: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0


class DryRun(BaseModel):
    assignment_id: int
    model: str
    reasoning_effort: str
    created_at: datetime
    materials_sha: str  # changes when the instructions or attachments do
    files_read: list[str] = Field(default_factory=list)
    files_unread: list[str] = Field(default_factory=list)
    task: str
    phases: list[Phase]
    done_looks_like: list[str]
    traps: list[Trap]
    effort: Effort
    effort_reasons: list[str]
    blocked: list[str]
    log: list[LogEntry]
    usage: Usage


class Materials(BaseModel):
    text: str
    sha: str
    files_read: list[str]
    files_unread: list[str]


# ---- inputs -----------------------------------------------------------------------------------


def norm(text: str) -> str:
    """Case, whitespace and curly quotes folded, for quote and criterion matching."""
    text = text.translate(str.maketrans("‘’“”", "''\"\""))
    return " ".join(text.split()).casefold()


def _clip(value: object, n: int = LOG_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _file_text(path: Path) -> str:
    """Plain text of an attachment, or "" when there isn't any to read (images, scans, no pypdf)."""
    suffix = path.suffix.lower()
    if suffix in convert.TEXT:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix != ".pdf":
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf isn't installed, so %s is skipped: pip install -e \".[stretch]\"", path.name)
        return ""
    try:
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception as e:  # a broken PDF shouldn't stop the run
        log.warning("couldn't read %s: %s", path.name, e)
        return ""


def materials(conn: sqlite3.Connection, assignment: Assignment) -> Materials:
    """Everything the student is given, as plain text. Rubric points earned, rubric comments,
    submission comments and the score are left out on purpose (see the module docstring)."""
    courses = {c.id: c.name for c in db.list_courses(conn)}
    text, _, _ = canvas.parse_description(assignment.description_html)
    files, unreadable = prep._attachments(assignment)
    read, unread, parts = [], [link.label for link in unreadable], []
    for f in files:
        body = _file_text(f).strip()
        if body:
            read.append(f.name)
            parts.append(f"--- {f.name} ---\n{body[:ATTACHMENT_CHARS]}")
        else:
            unread.append(f.name)
    rubric = "\n".join(
        f"- {r.criterion} ({r.points_possible:g} pts)" if r.points_possible is not None else f"- {r.criterion}"
        for r in assignment.rubric
    )
    links = "\n".join(f"- {l.label}: {l.url}" for l in prep.known_links(assignment)) or "(none)"
    body = f"""Course: {courses.get(assignment.course_id, "")}
Assignment: {assignment.name}
Points: {assignment.points_possible if assignment.points_possible is not None else "unknown"}
How it's turned in: {prep.turn_in(assignment)}
Rubric criteria:
{rubric or "(no rubric)"}

Links (you can't open these):
{links}

Files the student has that you can't read:
{chr(10).join(f"- {name}" for name in unread) or "(none)"}

Instructions from the teacher:
{text or "(no description; work from the title and any attached files)"}

{chr(10).join(parts)}""".rstrip()
    return Materials(
        text=body,
        sha=hashlib.sha256(body.encode()).hexdigest()[:16],
        files_read=read,
        files_unread=unread,
    )


# ---- the model call ---------------------------------------------------------------------------


def _client() -> httpx.Client:
    """Like meta_client's, with a long read timeout: at high effort the stream can sit quiet for
    minutes while the model reasons. Tests swap this for a mock transport."""
    return httpx.Client(
        base_url=settings.meta_base_url,
        headers={"Authorization": f"Bearer {settings.meta_api_key}"},
        timeout=httpx.Timeout(30, read=900),
    )


def check_model(model: str) -> None:
    if model.endswith("-contributor"):
        raise MetaError(f"{model} is contributor tier, which trains on what it's sent. Use a standard model.")


def cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    p_in, p_cached, p_out = PRICES.get(model, PRICES[DEFAULT_MODEL])
    return ((input_tokens - cached_tokens) * p_in + cached_tokens * p_cached + output_tokens * p_out) / 1_000_000


def _tools(entries: list[LogEntry]) -> list[Tool]:
    """look_up and log_trouble. Each call is appended to `entries` as it happens; that log is what
    makes a trap 'observed' rather than recalled afterwards."""

    def record(kind: str, args: dict, detail_key: str) -> int:
        entries.append(
            LogEntry(n=len(entries) + 1, kind=kind, where=_clip(args.get("where")), detail=_clip(args.get(detail_key)))
        )
        return len(entries)

    def trouble(args: dict) -> dict:
        kind = args.get("kind") if args.get("kind") in ("stuck", "backtrack", "ambiguous") else "stuck"
        return {"logged": record(kind, args, "what_happened")}

    where = {"type": "string", "description": "Problem number, section or step"}
    return [
        Tool(
            name="look_up",
            description=(
                "Log something a student would have to look up outside the materials: a formula, "
                "definition, fact, source, a reading you weren't given, or the expected format."
            ),
            parameters={
                "type": "object",
                "properties": {"where": where, "what": {"type": "string", "description": "What you need, not its answer"}},
                "required": ["where", "what"],
                "additionalProperties": False,
            },
            fn=lambda args: {"logged": record("lookup", args, "what"), "note": LOOKUP_NOTE},
        ),
        Tool(
            name="log_trouble",
            description="Log the moment you get stuck, backtrack and redo something, or find an instruction ambiguous.",
            parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["stuck", "backtrack", "ambiguous"]},
                    "where": where,
                    "what_happened": {"type": "string", "description": "What went wrong, without the answer"},
                },
                "required": ["kind", "where", "what_happened"],
                "additionalProperties": False,
            },
            fn=trouble,
        ),
    ]


def _body(model: str, effort: str, tools: list[Tool]) -> dict:
    """Everything but `input`. Stable parts only, so each turn's prefix matches the last one's."""
    return {
        "model": model,
        "instructions": INSTRUCTIONS,
        "tools": [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
            for t in tools
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "ReportDraft",
                "schema": meta_client.json_schema(ReportDraft),
                "strict": False,
            }
        },
        "reasoning": {"effort": effort},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "start-helper-dry-run",
    }


def _user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def _count(client: httpx.Client, body: dict, history: list[dict]) -> int:
    """Input tokens for the first turn. Tokenizes only: no generation, no charge."""
    request = {"model": body["model"], "instructions": body["instructions"], "tools": body["tools"], "input": history}
    r = client.post("/responses/input_tokens", json=request)
    if r.status_code >= 400:
        raise MetaError(f"Token count {r.status_code}: {r.text[:300]}")
    return int(r.json().get("input_tokens", 0))


def _stream(client: httpx.Client, body: dict) -> dict:
    """POST /responses with stream=true and return the final response, which the terminal event
    carries (OpenAI's event shape, which Meta's API follows)."""
    final: dict | None = None
    with client.stream("POST", "/responses", json={**body, "stream": True}, headers={"Accept": "text/event-stream"}) as r:
        if r.status_code >= 400:
            raise MetaError(f"Meta API {r.status_code}: {r.read().decode(errors='replace')[:500]}")
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line.removeprefix("data: ")
            if data == "[DONE]":
                break
            event = json.loads(data)
            if event.get("type") == "error":
                raise MetaError(f"Meta API error: {event}")
            if event.get("type") in ("response.completed", "response.failed", "response.incomplete"):
                final = event.get("response") or {}
    if final is None:
        raise MetaError("The stream ended without a final response")
    if final.get("error"):
        raise MetaError(f"Meta API error: {final['error']}")
    if final.get("status") == "incomplete":
        raise MetaError(f"Response incomplete: {final.get('incomplete_details')}")
    return final


def _add_usage(usage: Usage, model: str, u: dict) -> None:
    usage.turns += 1
    usage.input_tokens += u.get("input_tokens", 0)
    usage.cached_tokens += (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
    usage.output_tokens += u.get("output_tokens", 0)
    usage.reasoning_tokens += (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0)
    usage.cost_usd = cost(model, usage.input_tokens, usage.cached_tokens, usage.output_tokens)


def _replay(output: list[dict]) -> list[dict]:
    """A response's output items in the form the next request takes them: reasoning with its
    encrypted content (one without it can't be replayed, so it's dropped), assistant text with its
    phase, and function calls."""
    items: list[dict] = []
    for item in output:
        kind = item.get("type")
        if kind == "reasoning" and item.get("encrypted_content"):
            items.append({"type": "reasoning", "summary": item.get("summary") or [], "encrypted_content": item["encrypted_content"]})
        elif kind == "message":
            text = [{"type": "output_text", "text": p.get("text", "")} for p in item.get("content") or [] if p.get("type") == "output_text"]
            message = {"type": "message", "role": "assistant", "content": text}
            if item.get("phase"):
                message["phase"] = item["phase"]
            items.append(message)
        elif kind == "function_call":
            items.append({"type": "function_call", "call_id": item["call_id"], "name": item["name"], "arguments": item.get("arguments") or "{}"})
    return items


def run(
    conn: sqlite3.Connection,
    assignment: Assignment,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    budget: float = DEFAULT_BUDGET,
) -> DryRun:
    """Attempt the assignment, save the result in `dry_runs` and return it."""
    check_model(model)
    if not meta_client.configured():
        raise MetaError("META_API_KEY is not set")
    mat = materials(conn, assignment)
    entries: list[LogEntry] = []
    tools = _tools(entries)
    by_name = {t.name: t for t in tools}
    body = _body(model, effort, tools)
    history = [_user(mat.text)]
    usage = Usage()
    with _client() as client:
        try:
            n: int | None = _count(client, body, history)
        except (MetaError, httpx.HTTPError) as e:
            n = None
            log.warning("token count failed, running anyway: %s", e)
        if n is not None:
            floor = cost(model, n, 0, 0)
            log.info("dry run input: %s tokens, $%.3f before any output", n, floor)
            if floor > budget:
                raise MetaError(f"Input alone costs ${floor:.2f}, over the ${budget:.2f} budget")
        for _ in range(MAX_TURNS):
            response = _stream(client, {**body, "input": history})
            _add_usage(usage, model, response.get("usage") or {})
            output = response.get("output") or []
            calls = [i for i in output if i.get("type") == "function_call"]
            if not calls:
                draft = meta_client._parse(meta_client.output_text(response), ReportDraft)
                result = _assemble(assignment, model, effort, mat, draft, entries, usage)
                save(conn, result)
                return result
            if usage.cost_usd >= budget:
                raise MetaError(
                    f"Stopped after {usage.turns} turns at ${usage.cost_usd:.2f}, over the ${budget:.2f} "
                    f"budget, with {len(entries)} log entries"
                )
            history += _replay(output)
            history += [
                {"type": "function_call_output", "call_id": c["call_id"], "output": meta_client._run_tool(by_name, c)}
                for c in calls
            ]
    raise MetaError(f"No report after {MAX_TURNS} turns (${usage.cost_usd:.2f} spent)")


def _assemble(
    assignment: Assignment,
    model: str,
    effort: str,
    mat: Materials,
    draft: ReportDraft,
    entries: list[LogEntry],
    usage: Usage,
) -> DryRun:
    """The stored run: the model's report plus code checks on each trap."""
    haystack = norm(mat.text)
    logged = {e.n for e in entries}
    traps = [
        Trap(
            **t.model_dump(),
            quote_found=bool(norm(t.quote)) and norm(t.quote) in haystack,
            observed=bool(logged & set(t.log_refs)),
        )
        for t in draft.traps[:MAX_TRAPS]
    ]
    return DryRun(
        assignment_id=assignment.id,
        model=model,
        reasoning_effort=effort,
        created_at=clock.real_now(),
        materials_sha=mat.sha,
        files_read=mat.files_read,
        files_unread=mat.files_unread,
        task=draft.task,
        phases=draft.phases,
        done_looks_like=draft.done_looks_like,
        traps=traps,
        effort=draft.effort,
        effort_reasons=draft.effort_reasons,
        blocked=draft.blocked,
        log=entries,
        usage=usage,
    )


def count(conn: sqlite3.Connection, assignment: Assignment, model: str = DEFAULT_MODEL) -> dict:
    """What a run would cost before spending anything: input tokens and the per-turn range."""
    check_model(model)
    if not meta_client.configured():
        raise MetaError("META_API_KEY is not set")
    mat = materials(conn, assignment)
    with _client() as client:
        n = _count(client, _body(model, DEFAULT_EFFORT, _tools([])), [_user(mat.text)])
    return {
        "input_tokens": n,
        "first_turn_input_usd": round(cost(model, n, 0, 0), 4),
        "later_turn_input_usd": round(cost(model, n, n, 0), 4),  # if the prefix is served from cache
        "max_output_usd_per_turn": round(cost(model, 0, 0, MAX_OUTPUT_TOKENS), 4),
        "files_read": mat.files_read,
        "files_unread": mat.files_unread,
    }


# ---- storage ----------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS dry_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL            -- DryRun as JSON; every run is kept, so models can be compared
);
"""


def save(conn: sqlite3.Connection, result: DryRun) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO dry_runs (assignment_id, created_at, data) VALUES (?, ?, ?)",
        (result.assignment_id, result.created_at.isoformat(), result.model_dump_json()),
    )
    conn.commit()


def latest(conn: sqlite3.Connection, assignment_id: int, model: str | None = None) -> DryRun | None:
    conn.executescript(SCHEMA)
    rows = conn.execute(
        "SELECT data FROM dry_runs WHERE assignment_id = ? ORDER BY id DESC", (assignment_id,)
    ).fetchall()
    runs = [DryRun.model_validate_json(r["data"]) for r in rows]
    return next((r for r in runs if model is None or r.model == model), None)


# ---- onto the card ----------------------------------------------------------------------------


def _lost_criteria(conn: sqlite3.Connection, assignment_id: int) -> set[str]:
    """Rubric criteria (normalized) this student has lost points on in the assignment's course."""
    a = db.get_assignment(conn, assignment_id)
    if a is None:
        return set()
    return {
        norm(e["criterion"])
        for e in student_model.feedback_evidence(conn, a.course_id, limit=100)
        if e["source"] == "rubric"
    }


def _personal(trap: Trap, lost: set[str]) -> bool:
    return bool(trap.rubric_criterion) and norm(trap.rubric_criterion) in lost


def ranked_traps(conn: sqlite3.Connection, result: DryRun) -> list[Trap]:
    """Traps in the order a card should show them: ones on a rubric criterion this student has lost
    points on before in the course first, then ones logged during the attempt, then ones whose
    quote checked out. Within a group, the model's order stands."""
    lost = _lost_criteria(conn, result.assignment_id)
    return sorted(result.traps, key=lambda t: (not _personal(t, lost), not t.observed, not t.quote_found))


def watch_out(conn: sqlite3.Connection, result: DryRun) -> str:
    traps = ranked_traps(conn, result)
    if not traps:
        return ""
    t = traps[0]
    return f"{t.where}: {t.what_happens} Start with: {t.first_step}"


def apply_to_card(conn: sqlite3.Connection, assignment_id: int) -> PrepCard:
    """Put the latest dry run's effort level on the card, and its top trap as the watch-out line
    when the card has none or the trap lands on a criterion the student has lost points on before.
    Otherwise the teacher-quoted line from past feedback stays."""
    card = db.get_card(conn, assignment_id)
    result = latest(conn, assignment_id)
    if card is None or result is None:
        raise ValueError(f"Assignment {assignment_id} needs both a card and a dry run")
    update: dict = {"effort": result.effort}
    traps = ranked_traps(conn, result)
    if traps and (not card.watch_out or _personal(traps[0], _lost_criteria(conn, assignment_id))):
        update["watch_out"] = watch_out(conn, result)
    card = card.model_copy(update=update)
    db.save_card(conn, card)
    return card


# ---- command line -----------------------------------------------------------------------------


def _print(result: DryRun) -> None:
    u = result.usage
    print(f"{result.task}\n")
    print(f"Effort: {result.effort}")
    for reason in result.effort_reasons:
        print(f"  - {reason}")
    print("\nWhat completing it looks like:")
    for i, p in enumerate(result.phases, 1):
        print(f"  {i}. {p.name}{'  [hardest]' if p.hardest else ''}")
        print(f"     do: {p.what_you_do}\n     produces: {p.produces}")
        if p.needs:
            print(f"     needs: {', '.join(p.needs)}")
    print("\nDone looks like:")
    for item in result.done_looks_like:
        print(f"  [ ] {item}")
    print("\nTraps:")
    for t in result.traps:
        checks = ", ".join(c for c, ok in (("quote found", t.quote_found), ("logged during attempt", t.observed)) if ok)
        print(f"  - [{t.kind}] {t.where}: {t.what_happens}" + (f"  ({checks})" if checks else "  (unchecked)"))
        if t.quote:
            print(f"    quote: \"{t.quote}\"")
        print(f"    first step: {t.first_step}")
        if t.rubric_criterion:
            print(f"    rubric: {t.rubric_criterion}")
    if result.blocked:
        print("\nCouldn't attempt here:")
        for b in result.blocked:
            print(f"  - {b}")
    print("\nLog during the attempt:")
    for e in result.log:
        print(f"  #{e.n} [{e.kind}] {e.where}: {e.detail}")
    if not result.log:
        print("  (nothing logged)")
    print(
        f"\n{result.model} at {result.reasoning_effort} effort, {u.turns} turns: {u.input_tokens:,} in "
        f"({u.cached_tokens:,} cached), {u.output_tokens:,} out ({u.reasoning_tokens:,} reasoning), ${u.cost_usd:.3f}"
    )
    if result.files_unread:
        print(f"Unread files: {', '.join(result.files_unread)}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m stretch.dry_run", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "count": "count input tokens and show the cost range (free)",
        "run": "attempt the assignment and save the result (spends money)",
        "show": "print the latest saved run",
        "apply": "put the latest run's effort and top trap on the assignment's card",
    }
    for name, help_text in commands.items():
        p = sub.add_parser(name, help=help_text)
        p.add_argument("assignment_id", type=int)
        if name in ("count", "run"):
            p.add_argument("--model", default=DEFAULT_MODEL, help=f"default {DEFAULT_MODEL}; muse-spark-1.3 costs less")
        if name == "run":
            p.add_argument("--effort", default=DEFAULT_EFFORT, choices=REASONING_EFFORTS)
            p.add_argument("--budget", type=float, default=DEFAULT_BUDGET, help="dollars; stops before a turn past it")
            p.add_argument("--force", action="store_true", help="run even if an unchanged assignment already has a run")
    args = parser.parse_args(argv)

    conn = db.connect()
    db.init_db(conn)
    assignment = db.get_assignment(conn, args.assignment_id)
    if assignment is None:
        sys.exit(f"No assignment {args.assignment_id} in {settings.db_path}. Sync first.")
    try:
        if args.command == "count":
            print(json.dumps(count(conn, assignment, args.model), indent=2))
        elif args.command == "run":
            previous = latest(conn, assignment.id, args.model)
            if previous and not args.force and previous.materials_sha == materials(conn, assignment).sha:
                print(f"Unchanged since the {previous.created_at:%b %d %H:%M} run; showing it (--force to rerun).\n")
                _print(previous)
                return
            _print(run(conn, assignment, args.model, args.effort, args.budget))
        elif args.command == "show":
            result = latest(conn, assignment.id)
            if result is None:
                sys.exit(f"No dry run for {assignment.name} yet: python -m stretch.dry_run run {assignment.id}")
            _print(result)
        else:
            card = apply_to_card(conn, assignment.id)
            print(f"Effort: {card.effort}\nWatch out: {card.watch_out or '(none)'}")
    except (MetaError, ValueError, httpx.HTTPError) as e:
        sys.exit(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
