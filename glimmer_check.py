"""Pre-flight for the decision layer: does a local Muse Glimmer behave the way the plan assumes?

Start llama-server with Muse Glimmer on 127.0.0.1:8080 (`--jinja`, one slot: `-np 1`), then run
`python glimmer_check.py` from the repo root. Each check prints its numbers, then one PASS/FAIL
line. Exit code is 1 if any check fails. The only things it touches are the local server and a
read-only copy of data/app.db, so no assignment text leaves the machine.

Prompts come from the server's own chat template (`/apply-template`) so the format matches what
Glimmer was trained on. The assistant turns are appended by hand, because the template has no way to
say "empty thinking message". The question comes from decisions/fields.py, the same wording the
labelling page shows, with each option given a letter.
"""

import argparse
import json
import math
import re
import secrets
import sqlite3
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import requests

from decisions.fields import ASSIGNMENT_FIELDS, Field

ROOT = Path(__file__).resolve().parent

MIN_BUILD = 10353  # first llama.cpp release that knows the muse-glimmer architecture
LETTERS = "ABCDEFG"
SPECIALS = ("<|start|>", "<|message|>", "<|eom|>", "<|eot|>")
THINK = "<|start|>assistant to=self<|message|><|eom|>"  # an empty thinking message
ANSWER = "<|start|>assistant to=user<|message|>"
MIN_LETTER_SHARE = 0.9  # check 3: how much probability the fast pass must put on the letters
CACHE_SLACK = 16  # check 4: tokens re-processed beyond the new part before it counts as a miss
SPEED_TOKENS = 1200  # check 5: the size of the "fresh" assignment prompt
PROBE_CHARS = 1000  # checks 3 and 4: description length of the default assignment

SYSTEM = (
    "You are the decision layer of Start Helper, a tool that turns a high school student's Canvas "
    "assignments into prep cards. You get one assignment and one multiple-choice question about "
    "it. Answer with the letter of the best option and nothing else.\n\n"
    "Reasoning strength: low."
)


# ---- assignments from the DB ------------------------------------------------------------------


class _Text(HTMLParser):
    """Canvas description HTML to plain text (stdlib only, so this script needs just requests)."""

    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    text = re.sub(r"[ \t ]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def load_assignments(db: Path) -> list[dict]:
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    courses = dict(conn.execute("SELECT id, name FROM courses"))
    rows = []
    for aid, course_id, data in conn.execute("SELECT id, course_id, data FROM assignments"):
        d = json.loads(data)
        rows.append({
            "id": aid,
            "course": courses.get(course_id, str(course_id)),
            "name": d["name"],
            "due": (d.get("due_at") or "")[:10],
            "points": d.get("points_possible"),
            "types": d.get("submission_types") or [],
            "text": html_to_text(d.get("description_html") or ""),
        })
    conn.close()
    return rows


def context(a: dict) -> str:
    points = "not graded" if a["points"] is None else f"{a['points']:g}"
    return "\n".join([
        f"Course: {a['course']}",
        f"Assignment: {a['name']}",
        f"Due: {a['due'] or 'no due date'}",
        f"Points: {points}",
        f"Submission: {', '.join(a['types']) or 'not stated'}",
        "Description:",
        a["text"] or "(none)",
    ])


def question_block(field: Field) -> str:
    last = LETTERS[len(field.options) - 1]
    lines = [f"Question: {field.question}"]
    lines += [f"{LETTERS[i]}. {o.label}" for i, o in enumerate(field.options)]
    lines.append(f"Answer with one letter, {LETTERS[0]} to {last}.")
    return "\n".join(lines)


# ---- the server -------------------------------------------------------------------------------


class Server:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.http = requests.Session()
        self.bos = "<|begin_of_text|>"

    def get(self, path: str) -> requests.Response:
        return self.http.get(self.url + path, timeout=30)

    def post(self, path: str, body: dict) -> dict:
        r = self.http.post(self.url + path, json=body, timeout=600)
        r.raise_for_status()
        return r.json()

    def tokenize(self, text: str, special: bool = True, pieces: bool = False) -> list:
        """Token ids (or {id, piece} dicts). special=True adds BOS the way /completion does."""
        body = {
            "content": text, "add_special": special, "parse_special": True, "with_pieces": pieces
        }
        return self.post("/tokenize", body)["tokens"]

    def detokenize(self, ids: list[int]) -> str:
        return self.post("/detokenize", {"tokens": ids})["content"]

    def render(self, messages: list[dict]) -> str:
        """The chat template's text for these turns, without BOS and without a generation prompt."""
        body = {"messages": messages, "add_generation_prompt": False}
        text = self.post("/apply-template", body)["prompt"]
        # /completion adds BOS itself, so the template's copy would be a second one
        return text.removeprefix(self.bos).removesuffix("<|start|>assistant")

    def complete(self, prompt: str, probs: int = 0, cache: bool = True, n_predict: int = 1) -> dict:
        body = {"prompt": prompt, "n_predict": n_predict, "temperature": 0, "cache_prompt": cache}
        if probs:
            body["n_probs"] = probs
        return self.post("/completion", body)


def fast_pass(
    server: Server, ctx: str, field: Field, think: bool = True, system: str = SYSTEM
) -> tuple[str, str]:
    """(shared prefix, probe tail) for one question. prefix + tail is the whole prompt.

    The prefix is everything that is the same for every question about this assignment, up to the
    start of the question, so the server can keep it cached while the tails change.
    """
    block = question_block(field)
    rendered = server.render([
        {"role": "system", "content": system},
        {"role": "user", "content": f"{ctx}\n\n{block}"},
    ])
    cut = rendered.index(block)
    return rendered[:cut], rendered[cut:] + (THINK if think else "") + ANSWER


def letter_probs(
    resp: dict, n_options: int
) -> tuple[dict[str, float], float, list[tuple[str, float]]]:
    """(probability per answer letter, probability in the top candidates, top 3 tokens)."""
    tops = resp["completion_probabilities"][0]["top_logprobs"]
    probs = {letter: 0.0 for letter in LETTERS[:n_options]}
    seen = []
    for t in tops:
        p = math.exp(t["logprob"])
        seen.append((t["token"], p))
        for letter in probs:
            if t["token"] in (letter, " " + letter):  # a letter may come with or without a space
                probs[letter] += p
    return probs, sum(p for _, p in seen), seen[:3]


def fmt(p: float) -> str:
    return f"{p:.2e}" if 0 < p < 1e-4 else f"{p:.4f}"


def lcp(a: list, b: list) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


# ---- the checks -------------------------------------------------------------------------------
# Each returns (ok, one-line summary) and prints its own numbers first.


def check_server(server: Server) -> tuple[bool, str]:
    health = server.get("/health")
    props = server.get("/props").json()
    build = props.get("build_info", "")
    match = re.match(r"b(\d+)", build)
    number = int(match.group(1)) if match else 0
    server.bos = props.get("bos_token") or server.bos
    settings = props.get("default_generation_settings", {})
    print(f"  /health {health.status_code} {health.text.strip()}")
    print(f"  /props build_info={build!r} model={props.get('model_alias')!r} "
          f"slots={props.get('total_slots')} n_ctx={settings.get('n_ctx')}")
    ok = health.status_code == 200 and number >= MIN_BUILD
    return ok, f"/health {health.status_code}, build b{number} (needs b{MIN_BUILD} or newer)"


def check_tokens(server: Server) -> tuple[bool, str]:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "Hello."}]
    sample = server.render(messages) + THINK + ANSWER  # no <|begin_of_text|> in it
    pieces = server.tokenize(sample, pieces=True)
    ids = [t["id"] for t in pieces]
    bad = []

    for s in SPECIALS:
        alone = server.tokenize(s, special=False)
        wanted = sample.count(s)
        found = ids.count(alone[0]) if len(alone) == 1 else 0
        print(f"  {s:<12} alone -> {alone}; in the sample prompt {found} of {wanted}")
        if len(alone) != 1 or found != wanted:
            bad.append(s)

    bos = server.tokenize(server.bos, special=False)
    doubled = server.tokenize(server.bos + sample)

    def leading(toks: list[int]) -> int:
        return next((i for i, t in enumerate(toks) if t != bos[0]), len(toks))

    print(f"  BOS {server.bos} = {bos}; sample starts with {leading(ids)} BOS "
          f"({ids.count(bos[0])} in all); the same text with BOS typed in starts with "
          f"{leading(doubled)}")
    if len(bos) != 1 or leading(ids) != 1 or ids.count(bos[0]) != 1:
        bad.append("BOS")

    letters = []
    for letter in LETTERS:
        bare = server.tokenize(letter, special=False)
        spaced = server.tokenize(" " + letter, special=False)
        letters.append(f"{letter}={bare[0] if len(bare) == 1 else bare}"
                       f"/{spaced[0] if len(spaced) == 1 else spaced}")
        if len(bare) != 1 or len(spaced) != 1:
            bad.append(letter)
    eom = server.tokenize("<|eom|>", special=False)
    print(f"  <|eom|> id {eom}; letter ids (bare/with leading space): {' '.join(letters)}")
    return not bad, "special tokens, BOS and letters" if not bad else f"problems with {bad}"


def check_probe(server: Server, a: dict) -> tuple[bool, str]:
    field = ASSIGNMENT_FIELDS[0]
    ctx = context(a)
    shares = {}
    runs = {}
    for think in (True, False):
        head, tail = fast_pass(server, ctx, field, think)
        resp = server.complete(head + tail, probs=20)
        runs[think] = letter_probs(resp, len(field.options))
    print(f"  assignment {a['id']}: {a['name'][:40]!r}, {len(a['text'])} characters of description")
    print(f"  {'':<4}{'option':<30}{'empty thinking':>16}{'no thinking':>14}")
    for i, option in enumerate(field.options):
        letter = LETTERS[i]
        print(f"  {letter:<4}{option.value:<30}{fmt(runs[True][0][letter]):>16}"
              f"{fmt(runs[False][0][letter]):>14}")
    for think, label in ((True, "empty thinking"), (False, "no thinking")):
        probs, top20, top3 = runs[think]
        shares[think] = sum(probs.values())
        shown = ", ".join(f"{t!r} {p:.3f}" for t, p in top3)
        print(f"  {label}: letters hold {shares[think]:.4f} of the probability "
              f"(top 20 hold {top20:.4f}); top tokens {shown}")
    ok = shares[True] >= MIN_LETTER_SHARE
    return ok, (f"fast pass puts {shares[True]:.4f} on A-{LETTERS[len(field.options) - 1]} "
                f"(needs {MIN_LETTER_SHARE}); without the thinking message {shares[False]:.4f}")


def continuation(server: Server, n: int) -> str:
    """Plain text that tokenizes to about n tokens, standing in for a model's continuation."""
    filler = ("The student should start by reading the instructions once, slowly, then list what "
              "has to be handed in and which part is worth the most points. ") * 8
    ids = server.tokenize(filler, special=False)[:n]
    return " " + server.detokenize(ids)


def check_cache(server: Server, ctx: str) -> tuple[bool, str]:
    # llama-server keeps every earlier prompt in a RAM cache (--cache-ram) and reloads the best
    # match, and /slots/0?action=erase needs --slot-save-path. A run tag at the very start of the
    # system turn makes P new to the server without touching anything else about it.
    system = f"Run tag {secrets.token_hex(4)}.\n{SYSTEM}"
    head, tail_a = fast_pass(server, ctx, ASSIGNMENT_FIELDS[0], system=system)
    head_b, tail_b = fast_pass(server, ctx, ASSIGNMENT_FIELDS[2], system=system)
    if head_b != head:
        return False, "the shared prefix is different for two questions about the same assignment"
    steps = [
        ("P", head),
        ("P + tail A (type)", head + tail_a),
        ("P + tail B (effort)", head + tail_b),
        ("P + 60-token continuation", head + continuation(server, 60)),
    ]
    p_tokens = server.tokenize(head)
    print(f"  P is {len(p_tokens)} tokens and starts with a run tag, so the first request is cold")
    print(f"  {'request':<28}{'prompt':>7}{'reused':>8}{'processed':>11}{'new part':>10}"
          f"{'extra':>7}")
    worst = 0
    for i, (label, prompt) in enumerate(steps):
        t = server.complete(prompt)["timings"]
        tokens = server.tokenize(prompt)
        new = len(tokens) - lcp(p_tokens, tokens) if i else len(tokens)
        extra = t["prompt_n"] - new
        if i:
            worst = max(worst, extra)
        print(f"  {label:<28}{len(tokens):>7}{t['cache_n']:>8}{t['prompt_n']:>11}{new:>10}"
              f"{extra if i else '-':>7}")
    ok = worst <= CACHE_SLACK
    return ok, (f"after P only the new part was processed (worst extra {worst} tokens, "
                f"allowed {CACHE_SLACK})" if ok else
                f"{worst} tokens beyond the new part were re-processed; the prefix is not kept "
                "(for a sliding-window model try --swa-full)")


def pick_speed(server: Server, rows: list[dict]) -> tuple[dict, str, int]:
    """The assignment whose fast-pass prompt is closest to SPEED_TOKENS tokens."""
    best = None
    for a in sorted(rows, key=lambda r: -len(r["text"]))[:12]:
        head, tail = fast_pass(server, context(a), ASSIGNMENT_FIELDS[0])
        n = len(server.tokenize(head + tail))
        if best is None or abs(n - SPEED_TOKENS) < abs(best[2] - SPEED_TOKENS):
            best = (a, head + tail, n)
    return best


def check_speed(server: Server, rows: list[dict]) -> tuple[bool, str]:
    a, prompt, n = pick_speed(server, rows)
    field = ASSIGNMENT_FIELDS[0]
    short = "" if n >= 0.9 * SPEED_TOKENS else f" (longest available; none reaches {SPEED_TOKENS})"
    print(f"  assignment {a['id']}: {a['name'][:40]!r}, prompt {n} tokens{short}")
    rates = []
    for run in (1, 2):  # cache off, so both runs are a full prompt
        resp = server.complete(prompt, probs=20, cache=False)
        t = resp["timings"]
        rates.append(t["prompt_per_second"])
        print(f"  fresh prompt, run {run}: {t['prompt_n']} tokens in {t['prompt_ms']:.0f} ms = "
              f"{t['prompt_per_second']:.0f} tokens/s")
    fast_ms = resp["timings"]["prompt_ms"]
    probs = letter_probs(resp, len(field.options))[0]
    fast_letter = max(probs, key=probs.get)

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"{context(a)}\n\n{question_block(field)}"},
    ]
    start = time.perf_counter()
    r = server.post("/v1/chat/completions", {
        "model": "muse-glimmer-30B",
        "messages": messages,
        "max_tokens": 4096,
        "cache_prompt": False,
        "chat_template_kwargs": {"reasoning_strength": "low"},
    })
    wall = time.perf_counter() - start
    choice = r["choices"][0]
    message = choice["message"]
    answer = (message.get("content") or "").strip()
    thinking = len(message.get("reasoning_content") or "")
    generated = r["usage"]["completion_tokens"]
    speed = r.get("timings", {}).get("predicted_per_second", 0)
    print(f"  normal answer via /v1/chat/completions (reasoning strength low, cache off): "
          f"{wall:.1f} s, {generated} tokens generated ({speed:.0f} tokens/s), {thinking} "
          f"characters of thinking, finish_reason {choice['finish_reason']}, "
          f"answer {answer[:20]!r}")
    print(f"  fast pass (one token, empty thinking) {fast_ms:.0f} ms vs {wall * 1000:.0f} ms "
          f"normally = {wall * 1000 / max(fast_ms, 1):.0f}x; fast pass picked {fast_letter}, "
          f"normal answer {answer[:1] or '-'}")
    valid = choice["finish_reason"] == "stop" and answer[:1] in LETTERS[:len(field.options)]
    return valid, (f"prompt {rates[-1]:.0f} tokens/s on {n} fresh tokens; normal answer "
                   f"{wall:.1f} s vs fast pass {fast_ms / 1000:.2f} s")


# ---- driver -----------------------------------------------------------------------------------


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # assignment titles have emoji
    ap = argparse.ArgumentParser(description="Check a local llama-server running Muse Glimmer.")
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "app.db")
    ap.add_argument("--assignment-id", type=int,
                    help="assignment for checks 3 and 4 "
                         f"(default: description nearest {PROBE_CHARS} characters)")
    args = ap.parse_args()

    server = Server(args.url)
    failed = []

    def run(n: int, name: str, check, *a) -> None:
        print(f"\n{n}. {name}")
        try:
            ok, summary = check(*a)
        except Exception as e:  # one broken check should not hide the others
            ok, summary = False, f"crashed: {type(e).__name__}: {e}"
        print(f"{'PASS' if ok else 'FAIL'}  {n}. {name}: {summary}")
        if not ok:
            failed.append(n)

    run(1, "Server and build", check_server, server)
    if failed:
        print("\nServer is not usable, so the other checks were skipped.")
        return 1
    run(2, "Tokens", check_tokens, server)

    try:
        rows = load_assignments(args.db)
    except (sqlite3.Error, OSError) as e:
        rows = []
        print(f"\nCould not read assignments from {args.db}: {e}")
    texty = [r for r in rows if r["text"]]
    if args.assignment_id is not None:
        chosen = [r for r in rows if r["id"] == args.assignment_id]
    else:
        chosen = sorted(texty, key=lambda r: abs(len(r["text"]) - PROBE_CHARS))[:1]

    if not chosen or not texty:
        for n, name in ((3, "Probe"), (4, "Cache rollback"), (5, "Speed")):
            print(f"\nFAIL  {n}. {name}: no usable assignment in {args.db}")
            failed.append(n)
    else:
        run(3, "Probe: letter probabilities", check_probe, server, chosen[0])
        run(4, "Cache rollback", check_cache, server, context(chosen[0]))
        run(5, "Speed", check_speed, server, texty)

    print(f"\n{'All checks passed.' if not failed else 'Failed: ' + ', '.join(map(str, failed))}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
