"""Loads settings from environment variables / .env into one Settings object.

Everything else imports `settings` from here instead of reading os.environ. Tests swap values with
`object.__setattr__(settings, "db_path", ...)` since the dataclass is frozen.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    canvas_base_url: str = os.getenv("CANVAS_BASE_URL", "").rstrip("/")
    canvas_token: str = os.getenv("CANVAS_TOKEN", "")
    meta_api_key: str = os.getenv("META_API_KEY", "")
    meta_model: str = os.getenv("META_MODEL", "muse-spark-1.3")
    meta_base_url: str = os.getenv("META_BASE_URL", "https://api.meta.ai/v1").rstrip("/")
    db_path: str = os.getenv("DB_PATH", "data/app.db")
    files_dir: str = os.getenv("FILES_DIR", "data/files")  # downloaded attachments + test photos
    vapid_public_key: str = os.getenv("VAPID_PUBLIC_KEY", "")
    vapid_private_key: str = os.getenv("VAPID_PRIVATE_KEY", "")
    vapid_contact: str = os.getenv("VAPID_CONTACT", "")
    # Scheduling. TIMEZONE is an IANA name like America/Los_Angeles; empty means the machine's own.
    timezone: str = os.getenv("TIMEZONE", "")
    work_start_hour: int = int(os.getenv("WORK_START_HOUR", "15"))
    work_end_hour: int = int(os.getenv("WORK_END_HOUR", "21"))
    # Background loop: Canvas poll interval, how far ahead to build cards, and a per-sync cap
    # on model calls so a first sync over a whole semester can't burn the credit.
    background: bool = _bool("BACKGROUND", True)
    sync_minutes: int = int(os.getenv("SYNC_MINUTES", "5"))
    card_horizon_days: int = int(os.getenv("CARD_HORIZON_DAYS", "14"))
    max_cards_per_sync: int = int(os.getenv("MAX_CARDS_PER_SYNC", "5"))


settings = Settings()
