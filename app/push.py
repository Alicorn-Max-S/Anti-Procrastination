"""Web push: stores browser subscriptions and sends notifications with pywebpush/VAPID.

Phone push needs HTTPS (Cloudflare tunnel or small server); desktop Chrome and Edge also accept
http://localhost. VAPID keys come from .env when set there (`python -m app.push` prints a pair);
otherwise `ensure_keys` makes a pair the first time the student turns notifications on and keeps
it in the kv table. Without keys, or without pywebpush installed, sending is a no-op and the
page's own desktop notifications are the fallback.
"""

import base64
import json
import logging
import sqlite3

from app import db
from app.config import settings

log = logging.getLogger(__name__)


def keys(conn: sqlite3.Connection) -> tuple[str, str]:
    """(public, private) VAPID keys: the .env pair if set, else the one ensure_keys stored."""
    if settings.vapid_public_key and settings.vapid_private_key:
        return settings.vapid_public_key, settings.vapid_private_key
    return db.kv_get(conn, "vapid_public_key") or "", db.kv_get(conn, "vapid_private_key") or ""


def configured(conn: sqlite3.Connection) -> bool:
    return all(keys(conn))


def ensure_keys(conn: sqlite3.Connection) -> str:
    """The public key, generating and storing a pair first if there is none. "" if that fails."""
    if not configured(conn):
        try:
            public, private = generate_keys()
        except ImportError:
            log.warning("pywebpush isn't installed; push stays off")
            return ""
        db.kv_set(conn, "vapid_public_key", public)
        db.kv_set(conn, "vapid_private_key", private)
    return keys(conn)[0]


def save_subscription(conn: sqlite3.Connection, subscription: dict) -> None:
    """Store a browser push subscription (the JSON from PushSubscription.toJSON())."""
    if not subscription.get("endpoint"):
        raise ValueError("subscription has no endpoint")
    db.save_subscription(conn, subscription)


def send_ping(
    conn: sqlite3.Connection,
    title: str,
    body: str,
    url: str = "/",
    tag: str = "",
    extra: dict | None = None,
    skip: str = "",
) -> int:
    """Send a push notification to all stored subscriptions except the one whose endpoint is
    `skip`. Returns how many went out.

    `extra` rides along in the payload for sw.js: `kind` ("ping" | "missed" | "answered"),
    `block_id` (for the Start now / +15 min buttons), `renotify` (alert again under the same tag)
    and `clear` (the tags an "answered" push replaces).
    """
    public_key, private_key = keys(conn)
    if not (public_key and private_key):
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.warning("pywebpush isn't installed; skipping push")
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag, **(extra or {})})
    sent = 0
    for sub in db.list_subscriptions(conn):
        if skip and sub["endpoint"] == skip:
            continue
        try:
            webpush(
                sub,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": settings.vapid_contact or "mailto:admin@localhost"},
                ttl=3600,
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):  # the browser dropped this subscription
                db.delete_subscription(conn, sub["endpoint"])
            else:
                log.warning("push failed (%s): %s", status, e)
        except Exception as e:  # a network hiccup must never break the start loop
            log.warning("push failed: %s", e)
    return sent


def generate_keys() -> tuple[str, str]:
    """A fresh VAPID key pair as (public, private), both base64url without padding."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01

    v = Vapid01()
    v.generate_keys()
    raw_private = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    raw_public = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    return b64(raw_public), b64(raw_private)


if __name__ == "__main__":
    public, private = generate_keys()
    print("Paste into .env:\n")
    print(f"VAPID_PUBLIC_KEY={public}")
    print(f"VAPID_PRIVATE_KEY={private}")
