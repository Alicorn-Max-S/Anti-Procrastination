"""The fast pass: a forward probe with empty thinking, then, only if it clears the fast line, the
same question with the options reversed. Acting needs both to pick the same option."""

from dataclasses import dataclass

from .llama import Llama
from .probe import Probe, clears, probe
from .prompts import Choice, Prompt


@dataclass
class FastPass:
    prompt: Prompt  # the forward prompt, which the thinking loop goes on from
    forward: Probe
    reversed: Probe | None  # None when the forward probe already failed
    instant: bool

    @property
    def flipped(self) -> bool | None:
        return None if self.reversed is None else self.reversed.top != self.forward.top


def fast_pass(
    llama: Llama, context: str, choice: Choice, fast_line: float, share_min: float,
    fast_format: str, slot: int = 0,
) -> FastPass:
    prompt = Prompt(llama, context, choice.block(), fast_format)
    forward = probe(llama, prompt.answer([]), choice, slot)
    if not clears(forward, fast_line, share_min):
        return FastPass(prompt, forward, None, False)
    flipped = choice.reversed()
    back = probe(llama, Prompt(llama, context, flipped.block(), fast_format).answer([]), flipped, slot)
    instant = back.top == forward.top and back.letter_share >= share_min
    return FastPass(prompt, forward, back, instant)
