"""Web push: stores browser subscriptions and sends notifications with pywebpush/VAPID.

Phone push needs HTTPS (Cloudflare tunnel or small server). Without VAPID keys, or without
pywebpush installed, sending is a no-op and the page's own desktop notifications are the fallback.
Generate keys once with `python -m app.push` and paste them into .env.
"""

import base64
import json
import logging
import sqlite3

from app import db
from app.config import settings

log = logging.getLogger(__name__)


def configured() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def save_subscription(conn: sqlite3.Connection, subscription: dict) -> None:
    """Store a browser push subscription (the JSON from PushSubscription.toJSON())."""
    if not subscription.get("endpoint"):
        raise ValueError("subscription has no endpoint")
    db.save_subscription(conn, subscription)


def send_ping(conn: sqlite3.Connection, title: str, body: str, url: str = "/", tag: str = "") -> int:
    """Send a push notification to all stored subscriptions. Returns how many went out."""
    if not configured():
        return 0
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        log.warning("pywebpush isn't installed; skipping push")
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent = 0
    for sub in db.list_subscriptions(conn):
        try:
            webpush(
                sub,
                data=payload,
                vapid_private_key=settings.vapid_private_key,
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
