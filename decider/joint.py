"""Joint mode for assignments: every field in one prompt and one thinking chain, with a probe per
field after each paragraph (settings.mode "joint").

The prompt is base Glimmer's (eval/compare.py, setup 2): the system turn for a few questions, the
item, every question block, and the reply form "type: <letter>", one line per field. A field's probe
opens the answer turn, writes the earlier fields' answers in that form, then "<field>:", and reads
the letter probabilities. So template and effort see a type answer the way base Glimmer's own reply
does, and with no second stage. Probes branch off the cached chain: a round of three costs about a
tenth of a second, a paragraph of thinking about 2.6 s, so one shared chain replaces up to three.
"""

from collections.abc import Sequence

from .llama import Llama
from .probe import Probe, probe, shifted
from .prompts import ANSWER, LETTERS, REPLY, SYSTEM, THINK_CLOSE, THINK_OPEN, Choice

# One message with all of an item's questions. Same role text as the per-field system turn, which
# expects a single question, so only the part about how many questions and how to answer changes.
MULTI_SYSTEM = SYSTEM.replace(
    "and one multiple-choice question about it. Answer with the letter of the best option and nothing else.",
    "and a few multiple-choice questions about it. Answer each question with the letter of its best option.",
)
assert MULTI_SYSTEM != SYSTEM, "decider SYSTEM text changed: update MULTI_SYSTEM"


def questions_text(cs: Sequence[Choice]) -> str:
    """Every question block as the decider shows it, under its field name, without its reply line."""
    return "\n\n".join(f"[{c.field}]\n" + c.block().removesuffix("\n" + REPLY) for c in cs)


def user_text(context: str, cs: Sequence[Choice]) -> str:
    """The user turn base Glimmer and joint mode share: the item, the questions, the reply form."""
    form = "\n".join(f"{c.field}: <letter>" for c in cs)
    return f"{context}\n\n{questions_text(cs)}\n\nReply with one line per question, in this form:\n{form}"


class JointPrompt:
    """One item with all its question blocks, as token ids for the chain and for each field's probe."""

    def __init__(self, llama: Llama, context: str, choices: Sequence[Choice], fast_format: str) -> None:
        self.llama = llama
        self.context = context
        self.fast_format = fast_format
        self.choices = list(choices)
        rendered = llama.render([
            {"role": "system", "content": MULTI_SYSTEM},
            {"role": "user", "content": user_text(context, self.choices)},
        ])
        self.turns = llama.tokenize(rendered, bos=True)

    def reversed(self) -> "JointPrompt":
        """The same item with every question's options in reverse (the fast pass's second probe)."""
        return JointPrompt(self.llama, self.context, [c.reversed() for c in self.choices], self.fast_format)

    def thinking(self, thought: list[int]) -> list[int]:
        return self.turns + self.llama.const(THINK_OPEN) + thought

    def answer(self, thought: list[int], written: list[tuple[str, str]], field: str) -> list[int]:
        """The answer turn after the thinking so far, with the earlier fields' letters written in the
        reply form, up to "<field>:" (the letter comes next)."""
        if not thought and self.fast_format == "no-thinking":
            head = self.turns + self.llama.const(ANSWER)
        else:
            head = self.thinking(thought) + self.llama.const(THINK_CLOSE + ANSWER)
        return head + self.llama.const("".join(f"{f}: {letter}\n" for f, letter in written) + f"{field}:")


def letter(c: Choice, value: str) -> str:
    """The letter an option has in this choice's order."""
    index = next(i for i, o in enumerate(c.options) if o.value == value)
    return LETTERS[c.order.index(index)]


def probe_round(llama: Llama, prompt: JointPrompt, thought: list[int], written: dict[str, str],
                ask: set[str], slot: int = 0,
                offsets: dict[str, Sequence[float] | None] | None = None) -> dict[str, Probe]:
    """A probe for each field in `ask`, in question order. Every earlier field is written first:
    its value in `written` when it has one, else its top option in this round. Offsets (the fast
    pass's, by field) apply before a top option is written for the fields after it."""
    out: dict[str, Probe] = {}
    lines: list[tuple[str, str]] = []
    last = max((i for i, c in enumerate(prompt.choices) if c.field in ask), default=-1)
    for c in prompt.choices[:last + 1]:
        if c.field in ask:
            p = shifted(probe(llama, prompt.answer(thought, lines, c.field), c, slot), (offsets or {}).get(c.field))
            out[c.field] = p
            value = c.options[p.top].value
        else:
            value = written[c.field]
        lines.append((c.field, letter(c, value)))
    return out
