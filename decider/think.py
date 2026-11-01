"""The thinking loop: Glimmer thinks a paragraph at a time, with a probe after each paragraph, until
the probe clears the think line, Glimmer closes its thinking, or the cap is reached.

A paragraph is generated up to "\\n\\n" or PARAGRAPH_MAX tokens. A paragraph under MIN_PROBE_CHUNK
tokens gets another chunk before it is probed. A new paragraph starts only while the thinking is
under CAP_TOKENS; the one in progress always finishes. It never stops because probes agree.
"""

from dataclasses import dataclass, field

from .config import Settings
from .llama import Llama
from .probe import Probe, clears, probe
from .prompts import THINK_CLOSE, Choice, Prompt

PARAGRAPH_STOP = "\n\n"


@dataclass
class Thought:
    tokens: list[int] = field(default_factory=list)
    trace: list[tuple[int, Probe]] = field(default_factory=list)  # (thinking tokens, probe)
    finished: bool = False  # Glimmer ended its thinking itself (<|eom|> or <|eot|>)
    clear: bool = False  # the last probe cleared the think line

    @property
    def last(self) -> Probe:
        return self.trace[-1][1]


def _chunk(llama: Llama, prompt: Prompt, thought: list[int], s: Settings, slot: int,
           seed: int | None) -> tuple[list[int], bool]:
    """One generation call: (the new token ids, whether Glimmer ended its thinking)."""
    body = {
        "prompt": prompt.thinking(thought),
        "n_predict": s.paragraph_max,
        "stop": [PARAGRAPH_STOP],
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 64,
        "min_p": 0.0,  # llama-server defaults to 0.05; the spec's sampler has none
        "return_tokens": True,
        "cache_prompt": True,
        "id_slot": slot,
    }
    if seed is not None:
        body["seed"] = seed
    resp = llama.completion(body, kind="think")
    ids = list(resp.get("tokens") or [])
    ends = {llama.special(THINK_CLOSE), llama.special("<|eot|>")}
    cut = next((i for i, t in enumerate(ids) if t in ends), None)
    if cut is not None:  # Glimmer closed its thinking (and may have gone on to answer): drop that
        return ids[:cut], True
    if resp.get("stop_type") == "eos" or not ids:  # some other end-of-generation token
        return ids[:-1], True
    if resp.get("stop_type") == "word" and not llama.detokenize(ids).endswith(PARAGRAPH_STOP):
        ids += llama.const(PARAGRAPH_STOP)  # the server left the stop text out: put it back
    return ids, False


def think(llama: Llama, prompt: Prompt, choice: Choice, line: float, s: Settings, slot: int = 0,
          seed: int | None = None) -> Thought:
    t = Thought()
    while len(t.tokens) < s.cap_tokens:  # a new paragraph starts only under the cap
        paragraph: list[int] = []
        while True:  # the paragraph in progress always finishes
            ids, t.finished = _chunk(llama, prompt, t.tokens + paragraph, s, slot, seed)
            paragraph += ids
            if t.finished or len(paragraph) >= s.min_probe_chunk:
                break
        t.tokens += paragraph
        p = probe(llama, prompt.answer(t.tokens), choice, slot)
        t.trace.append((len(t.tokens), p))
        t.clear = clears(p, line, s.letter_share_min)
        if t.clear or t.finished:
            break
    return t
