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
from urllib.parse import urlparse

from app import db
from app.clock import clock
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
    """Store a browser push subscription (the JSON from PushSubscription.toJSON()), with the
    `device` the page says it is ("Chrome on Android") and when it last sent it (every load)."""
    if not subscription.get("endpoint"):
        raise ValueError("subscription has no endpoint")
    device = str(subscription.get("device") or "")[:60]
    db.save_subscription(conn, {**subscription, "device": device, "seen_at": clock.real_now().isoformat()})


# The browser behind each push service, by the subscription endpoint's host.
SERVICES = {
    "fcm.googleapis.com": "Chrome (or another browser on Google's push)",
    "push.services.mozilla.com": "Firefox",
    "notify.windows.com": "Edge",
    "push.apple.com": "Safari",
}

LATE = (
    "Firefox on Android holds pings while the phone is asleep and shows them when you next open "
    "it. Use Chrome on the phone for pings on time."
)


def devices(conn: sqlite3.Connection) -> list[dict]:
    """Where pings go, for the dashboard: each browser's name, when it last opened the app, and a
    `warning` when its pings come late. One that subscribed before the page sent its name is
    known only by its push service until it opens the app again."""
    out = []
    for sub in db.list_subscriptions(conn):
        name = sub.get("device") or ""
        warning = LATE if name == "Firefox on Android" else ""
        if not name:
            host = urlparse(sub["endpoint"]).hostname or ""
            browser = next((b for h, b in SERVICES.items() if host == h or host.endswith("." + h)), "A browser")
            name = f"{browser}, device not known yet (named when it next opens the app)"
            warning = f"If this is Firefox on a phone: {LATE}" if browser == "Firefox" else ""
        out.append({"name": name, "seen_at": sub.get("seen_at"), "warning": warning})
    return out


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

    Everything the student should see goes out with Urgency: high. At the default (normal),
    Chrome on Android gets the push only once the phone wakes up, so a ping lands when the
    student next opens the phone, not at the start time. An "answered" push only tidies up, so it
    stays normal.
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
    urgency = "normal" if (extra or {}).get("kind") == "answered" else "high"
    sent = 0
    for sub in db.list_subscriptions(conn):
        if skip and sub["endpoint"] == skip:
            continue
        try:
            webpush(
                {"endpoint": sub["endpoint"], "keys": sub.get("keys", {})},
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": settings.vapid_contact or "mailto:admin@localhost"},
                ttl=3600,
                timeout=10,
                headers={"Urgency": urgency},
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
