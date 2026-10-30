"""Live progress of the running sync, for the sync bar: phase, counts, what's in flight, what failed.

Sync work runs on worker threads (sync.py fetches Canvas in parallel, prep.build_all_missing
builds several cards at once), so every update takes a lock. `snapshot()` is what /api/state
returns. A finished sync's snapshot is kept as the report (loop.py stores it in kv).
"""

import threading
import time
from datetime import datetime, timezone

PHASE_LABELS = {
    "courses": "Checking courses",
    "assignments": "Fetching assignments",
    "feedback": "Fetching teacher feedback",
    "cards": "Building cards",
    "planning": "Planning work blocks",
    "done": "Done",
}


class Progress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._phase = ""
            self._done = 0
            self._total = 0
            self._current: list[str] = []
            self._failures: list[dict] = []
            self._timings: dict[str, float] = {}
            self._phase_started = time.perf_counter()
            self._started = time.perf_counter()
            self._started_at = datetime.now(timezone.utc).isoformat()

    def phase(self, name: str, total: int = 0) -> None:
        """Start a new phase; the previous one's duration goes into the timings."""
        with self._lock:
            self._close_phase()
            self._phase, self._done, self._total, self._current = name, 0, total, []
            self._phase_started = time.perf_counter()

    def _close_phase(self) -> None:
        if self._phase:
            self._timings[self._phase] = round(time.perf_counter() - self._phase_started, 2)

    def begin(self, item: str) -> None:
        with self._lock:
            self._current.append(item)

    def end(self, item: str) -> None:
        with self._lock:
            if item in self._current:
                self._current.remove(item)
            self._done += 1

    def step(self, item: str = "") -> None:
        """One unit of the phase finished (when nothing tracks it as in flight)."""
        with self._lock:
            self._done += 1

    def fail(self, item: str, error: str, phase: str | None = None) -> None:
        with self._lock:
            self._failures.append({"phase": phase or self._phase, "name": item, "error": error[:300]})

    def finish(self) -> dict:
        with self._lock:
            self._close_phase()
            self._phase, self._current = "done", []
            self._timings["total"] = round(time.perf_counter() - self._started, 2)
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self._phase,
                "label": PHASE_LABELS.get(self._phase, self._phase),
                "done": self._done,
                "total": self._total,
                "current": list(self._current),
                "failures": list(self._failures),
                "timings": dict(self._timings),
                "started_at": self._started_at,
                "elapsed": round(time.perf_counter() - self._started, 1),
            }
