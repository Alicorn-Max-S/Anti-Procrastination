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
    parallel_slots: int = int(os.getenv("PARALLEL_SLOTS", "1"))
    cap_tokens: int = int(os.getenv("CAP_TOKENS", "512"))  # no new paragraph at this many
    paragraph_max: int = int(os.getenv("PARAGRAPH_MAX", "96"))
    min_probe_chunk: int = int(os.getenv("MIN_PROBE_CHUNK", "24"))
    letter_share_min: float = float(os.getenv("LETTER_SHARE_MIN", "0.5"))
    lines_file: str = os.getenv("LINES_FILE", "decisions/lines.json")
    prompt_version: str = os.getenv("PROMPT_VERSION", "1")
    # How the fast pass ends the prompt: an empty thinking message, or straight to the answer.
    # glimmer_check.py check 3 picks it (empty thinking put 0.998 on the letters, none 0.880).
    fast_pass_format: str = os.getenv("FAST_PASS_FORMAT", "empty-thinking")


settings = Settings()


@dataclass(frozen=True)
class Lines:
    """Per-field confidence lines and the cheap fields that skip thinking (decisions/lines.json)."""

    lines: dict[str, dict[str, float]]
    skip_thinking: frozenset[str]

    def fast(self, field: str) -> float:
        return self.lines.get(field, {}).get("fast", DEFAULT_LINE)

    def think(self, field: str) -> float:
        return self.lines.get(field, {}).get("think", DEFAULT_LINE)


def load_lines(path: str) -> Lines:
    """Read on every request, so approving new lines needs no restart. Missing file: defaults."""
    p = Path(path)
    p = p if p.is_absolute() else ROOT / p
    if not p.exists():
        return Lines({}, frozenset())
    data = json.loads(p.read_text(encoding="utf-8"))
    return Lines(data.get("lines", {}), frozenset(data.get("skip_thinking", [])))
