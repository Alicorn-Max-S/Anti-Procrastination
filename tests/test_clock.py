"""The simulated clock jumps and keeps ticking from the new time."""

from datetime import timedelta, timezone

from app.clock import Clock, local_tz
from app.config import settings
from tests.conftest import at


def test_real_by_default():
    c = Clock()
    assert not c.simulated
    assert abs((c.now() - c.real_now()).total_seconds()) < 1


def test_set_advance_reset():
    c = Clock()
    c.set(at(5, 16))
    assert c.simulated
    assert abs((c.now() - at(5, 16)).total_seconds()) < 1

    c.advance(timedelta(minutes=15))
    assert abs((c.now() - at(5, 16, 15)).total_seconds()) < 1

    c.reset()
    assert not c.simulated


def test_machine_zone_is_a_real_zone():
    """With TIMEZONE blank, the fallback must carry DST rules, not today's fixed UTC offset."""
    object.__setattr__(settings, "timezone", "")
    assert not isinstance(local_tz(), timezone)


def test_naive_times_are_local():
    c = Clock()
    c.set(at(5, 16).replace(tzinfo=None))
    assert abs((c.now() - at(5, 16)).total_seconds()) < 1
