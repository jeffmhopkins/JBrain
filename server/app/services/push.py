"""Web Push notifications (VAPID).

A pending review item (e.g. a share-link editor's proposal) fires a push to every
subscribed device so the owner gets a banner + home-screen badge even with the app
closed. Keys auto-generate on first boot into the `meta` KV (mirrors
ensure_access_key), so existing installs gain push with zero config.

Sends run on a daemon thread, POST-COMMIT, on the worker's own connection (same
pattern as services/image_analysis) — a push is an external network call and must
never block or fail the request transaction. The fresh pending count rides in the
payload so the service worker can set the badge without auth.
"""
from __future__ import annotations

import base64
import json
import threading

from ..config import get_settings
from ..db import get_conn, get_meta, set_meta
from . import reviews as reviews_svc

_PRIV_META = "vapid_private_pem"
_PUB_META = "vapid_public_key"   # base64url uncompressed P-256 point (applicationServerKey)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _generate_keypair() -> tuple[str, str]:
    """Return (private PKCS8 PEM, public applicationServerKey b64url)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    point = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )   # 65 bytes, 0x04-prefixed — exactly what the browser's applicationServerKey needs
    return pem, _b64url(point)


def ensure_vapid() -> None:
    """Seed the VAPID keypair on boot: env override is authoritative; else reuse
    the DB-stored pair; else generate one. Idempotent."""
    conn = get_conn()
    s = get_settings()
    if s.vapid_private_key.strip() and s.vapid_public_key.strip():
        set_meta(conn, _PRIV_META, s.vapid_private_key.strip())
        set_meta(conn, _PUB_META, s.vapid_public_key.strip())
        conn.commit()
        return
    if get_meta(_PRIV_META) and get_meta(_PUB_META):
        return
    pem, pub = _generate_keypair()
    set_meta(conn, _PRIV_META, pem)
    set_meta(conn, _PUB_META, pub)
    conn.commit()


def public_key() -> str:
    return get_meta(_PUB_META) or ""


# --- Subscriptions ----------------------------------------------------------

def upsert_subscription(conn, endpoint: str, p256dh: str, auth: str, ua: str | None) -> None:
    conn.execute(
        "INSERT INTO push_subscriptions (endpoint, p256dh, auth, ua) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, "
        "ua=excluded.ua, last_seen_at=datetime('now')",
        (endpoint, p256dh, auth, ua),
    )
    conn.commit()


def delete_subscription(conn, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.commit()


# --- Sending ----------------------------------------------------------------

def send_test(conn) -> dict:
    """Fire a test push to every subscribed device. Returns what was attempted so
    the UI can guide the user (how many devices, whether VAPID is configured)."""
    n = conn.execute("SELECT COUNT(*) AS c FROM push_subscriptions").fetchone()["c"]
    has_vapid = bool(get_meta(_PRIV_META))
    if n and has_vapid:
        notify_review_created("JBrain", "Test notification — push is working.")
    return {"subscriptions": n, "vapid": has_vapid}


def notify_review_created(title: str = "JBrain", body: str = "1 pending") -> None:
    """Fire-and-forget: spawn a worker that pushes to all subscriptions. Safe to
    call from inside a request handler AFTER its commit — never raises."""
    threading.Thread(target=_send_worker, args=(title, body), daemon=True).start()


def _send_worker(title: str, body: str) -> None:
    conn = get_conn()   # daemon thread → its own thread-local connection
    try:
        priv = get_meta(_PRIV_META)
        if not priv:
            return
        subs = conn.execute(
            "SELECT id, endpoint, p256dh, auth FROM push_subscriptions"
        ).fetchall()
        if not subs:
            return
        count = reviews_svc.pending_count(conn)
        # Generic payload — banners show on the lock screen, so no note titles or
        # names; just a count for the badge.
        payload = json.dumps({"title": title, "body": body, "count": count,
                              "url": "/shares", "tag": "jbrain-reviews"})
        try:
            from pywebpush import webpush, WebPushException
        except Exception:
            return   # dependency missing (not in the production image) — degrade silently
        subject = get_settings().vapid_subject
        dead: list[int] = []
        for s in subs:
            try:
                webpush(
                    subscription_info={"endpoint": s["endpoint"],
                                       "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                    data=payload, vapid_private_key=priv,
                    vapid_claims={"sub": subject},
                    timeout=10,
                )
            except WebPushException as exc:
                resp = getattr(exc, "response", None)
                if resp is not None and resp.status_code in (404, 410):
                    dead.append(s["id"])   # endpoint gone — prune
            except Exception:
                pass   # push service down / timeout — best-effort; resume-refresh recovers
        if dead:
            conn.executemany("DELETE FROM push_subscriptions WHERE id = ?", [(i,) for i in dead])
            conn.commit()
    except Exception:
        pass   # a notification must never break the request that triggered it
