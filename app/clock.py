"""Simulated clock so the demo can jump to '4:00 PM Thursday' without waiting.

Everything time-dependent (scheduler, pings, reflow) calls `clock.now()` and never datetime.now()
directly, so the demo can fast-forward. The simulated time is just an offset from real time, so it
keeps ticking forward after a jump. All datetimes are timezone-aware in the local zone.
"""

from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from tzlocal import get_localzone

from app.config import settings


def local_tz() -> tzinfo:
    """The student's timezone: TIMEZONE from settings, else this machine's zone.

    Always a real zone with DST rules (never a fixed UTC offset), so "3 PM" stays 3 PM on both
    sides of a clock change.
    """
    if settings.timezone:
        return ZoneInfo(settings.timezone)
    return get_localzone()


def as_local(dt: datetime) -> datetime:
    """Attach the local zone to a naive datetime, or convert an aware one into it."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=local_tz())
    return dt.astimezone(local_tz())


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta(0)

    @staticmethod
    def real_now() -> datetime:
        return datetime.now(local_tz())

    @property
    def simulated(self) -> bool:
        return self.offset != timedelta(0)

    def now(self) -> datetime:
        """Current time, including any simulated offset."""
        return self.real_now() + self.offset

    def set(self, when: datetime) -> None:
        """Pin 'now' to `when` (it keeps ticking forward from there)."""
        self.offset = as_local(when) - self.real_now()

    def advance(self, delta: timedelta) -> None:
        """Jump forward by `delta`."""
        self.offset += delta

    def reset(self) -> None:
        """Go back to real time."""
        self.offset = timedelta(0)


clock = Clock()
