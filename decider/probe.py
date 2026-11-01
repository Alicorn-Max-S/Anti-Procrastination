"""The probe: one token of output, and the probability on each option letter.

letter_share is how much of the probability lands on the option letters at all; confidence is the
top option's share of that.
"""

import math
from dataclasses import dataclass

from .llama import Llama
from .prompts import LETTERS, Choice

N_PROBS = 20


@dataclass(frozen=True)
class Probe:
    probs: tuple[float, ...]  # per option, in the field's own order (not the lettered order)
    letter_share: float
    confidence: float
    top: int  # option index of the most likely option
    probe_tokens: int  # prompt tokens llama-server processed for this probe


def letter_probs(top_logprobs: list[dict], n: int) -> list[float]:
    """Probability on each of the first n letters. A letter may come as "A" or " A"; both count."""
    index = {LETTERS[i]: i for i in range(n)}
    probs = [0.0] * n
    for t in top_logprobs:
        token = t["token"]
        i = index.get(token[1:] if token.startswith(" ") else token)
        if i is not None:
            probs[i] += t["prob"] if "prob" in t else math.exp(t["logprob"])
    return probs


def read_probe(resp: dict, choice: Choice) -> Probe:
    shown = letter_probs(resp["completion_probabilities"][0]["top_logprobs"], len(choice.order))
    probs = [0.0] * len(choice.options)
    for pos, i in enumerate(choice.order):
        probs[i] = shown[pos]
    share = sum(probs)
    top = max(range(len(probs)), key=probs.__getitem__)
    confidence = probs[top] / share if share > 0 else 0.0
    return Probe(tuple(probs), share, confidence, top, resp.get("timings", {}).get("prompt_n", 0))


def probe(llama: Llama, prompt: list[int], choice: Choice, slot: int) -> Probe:
    body = {
        "prompt": prompt,
        "n_predict": 1,
        "n_probs": N_PROBS,
        "temperature": 0,
        "cache_prompt": True,
        "id_slot": slot,
    }
    return read_probe(llama.completion(body, kind="probe"), choice)


def clears(p: Probe, line: float, share_min: float) -> bool:
    return p.confidence >= line and p.letter_share >= share_min
