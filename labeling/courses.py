"""Which classes count as academic for the labeling pass (the rest are skipped)."""

import re

_NON_ACADEMIC = re.compile(r"\b(coach|library|community engagement|cas|internship)\b", re.IGNORECASE)


def is_academic(name: str) -> bool:
    return not _NON_ACADEMIC.search(name or "")
