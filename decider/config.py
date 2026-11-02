"""Decider settings from environment variables (and .env, like the app), plus the lines file.

Tests build their own Settings with dataclasses.replace instead of setting environment variables.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LINE = 0.85  # every field's fast and think line until calibration writes lines.json
FAST_PASS_FORMATS = ("empty-thinking", "no-thinking")


@dataclass(frozen=True)
class Settings:
    glimmer_url: str = os.getenv("GLIMMER_URL", "http://127.0.0.1:8080").rstrip("/")
    parallel_slots: int = int(os.getenv("PARALLEL_SLOTS", "4"))
    cap_tokens: int = int(os.getenv("CAP_TOKENS", "512"))  # no new paragraph at this many
    paragraph_max: int = int(os.getenv("PARAGRAPH_MAX", "96"))
    min_probe_chunk: int = int(os.getenv("MIN_PROBE_CHUNK", "24"))
    letter_share_min: float = float(os.getenv("LETTER_SHARE_MIN", "0.5"))
    lines_file: str = os.getenv("LINES_FILE", "decisions/lines.json")
    prompt_version: str = os.getenv("PROMPT_VERSION", "9")  # 2: option definitions; 3: examples; 4: video or audio; 5: Nov 1 rules; 6: v2 credit wording back; 7: type before template and effort, more Nov 1 rules; 8: Something else without "journal" or "ungraded"; 9: Something else template line (Dashboard for a participation grade added after the v9 run), in-class readings, tighter in-class effort rule
    # Skip the template question when the type settles it (prompts.TEMPLATE_FROM_TYPE). "0" turns it off.
    template_from_type: bool = os.getenv("TEMPLATE_FROM_TYPE", "1") == "1"
    # How the fast pass ends the prompt: an empty thinking message, or straight to the answer.
    # glimmer_check.py check 3 picks it (empty thinking put 0.998 on the letters, none 0.880).
    fast_pass_format: str = os.getenv("FAST_PASS_FORMAT", "empty-thinking")
    # How an assignment is decided. "fields": each field in its own prompt, with its own thinking.
    # "joint": every field in one prompt and one thinking chain, a probe per field (decider/joint.py),
    # on its own lines file. Test questions are always decided per field.
    mode: str = os.getenv("DECIDER_MODE", "fields")
    joint_lines_file: str = os.getenv("JOINT_LINES_FILE", "decisions/lines.joint.json")


settings = Settings()
MODES = ("fields", "joint")


@dataclass(frozen=True)
class Lines:
    """Per-field confidence lines, offsets and temperature, and the cheap fields that skip thinking
    (decisions/lines.json). A field's entry is {"fast", "think"} plus, once calibrate fits them,
    "offsets" ({option value: log offset}, for the fast pass), "temperature" (for a decision from the
    fast pass) and "think_temperature" (from a thinking probe)."""

    lines: dict[str, dict]
    skip_thinking: frozenset[str]

    def fast(self, field: str) -> float:
        return self.lines.get(field, {}).get("fast", DEFAULT_LINE)

    def think(self, field: str) -> float:
        return self.lines.get(field, {}).get("think", DEFAULT_LINE)

    def offsets(self, field: str, values: list[str]) -> tuple[float, ...] | None:
        """The field's fast-pass offsets in option order, or None when it has none."""
        got = self.lines.get(field, {}).get("offsets")
        return tuple(got.get(v, 0.0) for v in values) if got else None

    def temperature(self, field: str, thinking: bool = False) -> float:
        """For a decision from the fast pass, or from a thinking probe (thinking True)."""
        return self.lines.get(field, {}).get("think_temperature" if thinking else "temperature", 1.0)


def load_lines(path: str) -> Lines:
    """Read on every request, so approving new lines needs no restart. Missing file: defaults."""
    p = Path(path)
    p = p if p.is_absolute() else ROOT / p
    if not p.exists():
        return Lines({}, frozenset())
    data = json.loads(p.read_text(encoding="utf-8"))
    return Lines(data.get("lines", {}), frozenset(data.get("skip_thinking", [])))
