"""The probe: one token of output, and the probability on each option letter.

letter_share is how much of the probability lands on the option letters at all; confidence is the
top option's share of that.

Two fitted corrections come from decisions/lines.json (eval/calibrate.py fits both): per-option
offsets that undo a field's lean on the fast pass (credit leaned towards "none"), and temperatures
(one for fast-pass probes, one for thinking probes) that turn a decision's confidence into an honest
chance of being right.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .llama import Llama
from .prompts import LETTERS, Choice

N_PROBS = 20
CALIBRATION_FLOOR = 1e-4  # an option's least share of the letters before a temperature (tempered)


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


def shift(probs: Sequence[float], offsets: Sequence[float]) -> list[float]:
    """Each option's probability times e^offset, renormalized to the same letter share."""
    share = sum(probs)
    weights = [p * math.exp(b) for p, b in zip(probs, offsets)]
    total = sum(weights)
    return [share * w / total for w in weights] if total > 0 else list(probs)


def shifted(p: Probe, offsets: Sequence[float] | None) -> Probe:
    """The probe with the field's option offsets applied (no offsets: unchanged)."""
    if not offsets or p.letter_share <= 0:
        return p
    probs = shift(p.probs, offsets)
    top = max(range(len(probs)), key=probs.__getitem__)
    return Probe(tuple(probs), p.letter_share, probs[top] / p.letter_share, top, p.probe_tokens)


def tempered(probs: Sequence[float], temperature: float) -> list[float]:
    """The letter probabilities, normalized, each at least CALIBRATION_FLOOR, at a temperature
    (over 1 softens). The floor matters after thinking, where the other letters fall out of the top
    20 and read as exactly 0, which no temperature could soften."""
    share = sum(probs)
    weights = [max(p / share, CALIBRATION_FLOOR) ** (1 / temperature) for p in probs]
    total = sum(weights)
    return [w / total for w in weights]


def calibrated(probs: Sequence[float], temperature: float) -> float:
    """The top option's chance of being right, at the field's fitted temperature."""
    return max(tempered(probs, temperature)) if sum(probs) > 0 else 0.0
