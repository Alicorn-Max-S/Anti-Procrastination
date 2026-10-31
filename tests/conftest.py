"""Shared test setup: a throwaway DB per test, and no real Canvas, model or push calls ever."""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import db, gdocs, web
from app.clock import clock
from app.config import settings

TZ = ZoneInfo("America/Los_Angeles")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A fixed local time in November 2026 (Nov 2 is a Monday)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


@pytest.fixture(autouse=True)
def no_google(monkeypatch):
    """Google file fetches answer 404 unless a test installs its own fake."""
    monkeypatch.setattr(gdocs, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))


@pytest.fixture(autouse=True)
def no_web(monkeypatch):
    """Web article fetches answer 404 unless a test installs its own fake."""
    monkeypatch.setattr(web, "_client", lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))


@pytest.fixture(autouse=True)
def isolated(tmp_path):
    """Point settings at temp files and blank out every credential. Settings is frozen, hence
    object.__setattr__; the originals are put back afterwards."""
    overrides = {
        "db_path": str(tmp_path / "test.db"),
        "files_dir": str(tmp_path / "files"),
        "canvas_base_url": "https://canvas.test",
        "canvas_token": "",
        "meta_api_key": "",
        "vapid_public_key": "",
        "vapid_private_key": "",
        "timezone": "America/Los_Angeles",
        "work_start_hour": 15,
        "work_end_hour": 21,
        "background": False,
    }
    originals = {name: getattr(settings, name) for name in overrides}
    for name, value in overrides.items():
        object.__setattr__(settings, name, value)
    clock.reset()
    yield
    clock.reset()
    for name, value in originals.items():
        object.__setattr__(settings, name, value)


@pytest.fixture
def conn():
    c = db.connect()
    db.init_db(c)
    yield c
    c.close()
