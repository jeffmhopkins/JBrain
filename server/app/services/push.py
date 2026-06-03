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

_PRIV_META = "vapid_private_pem"   # holds the raw base64url private scalar (legacy name)
_PUB_META = "vapid_public_key"     # base64url uncompressed P-256 point (applicationServerKey)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _generate_keypair() -> tuple[str, str]:
    """Return (private scalar b64url, public applicationServerKey b64url) — the
    standard Web Push VAPID key formats that pywebpush/py_vapid consume."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    scalar = priv.private_numbers().private_value.to_bytes(32, "big")   # raw 32-byte private key
    point = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )   # 65 bytes, 0x04-prefixed — what the browser's applicationServerKey needs
    return _b64url(scalar), _b64url(point)


def ensure_vapid() -> None:
    """Seed the VAPID keypair on boot: env override is authoritative; else reuse a
    valid DB-stored pair; else generate one. Regenerates an old PEM-format key from
    a pre-fix build (pywebpush can't consume it). Idempotent."""
    conn = get_conn()
    s = get_settings()
    if s.vapid_private_key.strip() and s.vapid_public_key.strip():
        set_meta(conn, _PRIV_META, s.vapid_private_key.strip())
        set_meta(conn, _PUB_META, s.vapid_public_key.strip())
        conn.commit()
        return
    cur = get_meta(_PRIV_META)
    if cur and "BEGIN" not in cur and get_meta(_PUB_META):
        return   # already a valid raw-scalar key
    priv, pub = _generate_keypair()
    set_meta(conn, _PRIV_META, priv)
    set_meta(conn, _PUB_META, pub)
    conn.commit()
    if cur:
        print("[push] regenerated VAPID key in the correct format; devices will re-subscribe.", flush=True)


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

def send_test(conn, delay_seconds: int = 0) -> dict:
    """Fire a test push to every subscribed device, optionally after a delay (so
    the user can close the app and confirm closed-app delivery). The delay is
    server-side — it survives the app being backgrounded/closed. Returns what was
    attempted so the UI can guide the user."""
    n = conn.execute("SELECT COUNT(*) AS c FROM push_subscriptions").fetchone()["c"]
    has_vapid = bool(get_meta(_PRIV_META))
    delay = max(0, min(int(delay_seconds or 0), 300))   # clamp 0–5min
    if not (n and has_vapid):
        return {"subscriptions": n, "vapid": has_vapid, "delay": delay}
    title, body = "JBrain", "Test notification — push is working."
    if delay:
        t = threading.Timer(delay, lambda: notify_review_created(title, body))
        t.daemon = True; t.start()
        return {"subscriptions": n, "vapid": has_vapid, "delay": delay, "scheduled": True}
    # Immediate: send synchronously and report the REAL outcome so the UI/logs can
    # show exactly what the push service said.
    res = _send_all(conn, title, body)
    return {"subscriptions": n, "vapid": has_vapid, "delay": 0, **res}


def notify_review_created(title: str = "JBrain", body: str = "1 pending") -> None:
    """Fire-and-forget: spawn a worker that pushes to all subscriptions. Safe to
    call from inside a request handler AFTER its commit — never raises."""
    threading.Thread(target=lambda: _send_all(get_conn(), title, body), daemon=True).start()


def notify(title: str, body: str, url: str = "/") -> None:
    """Fire-and-forget notification: post it to the in-app review bell AND push to
    every device with a deep-link. Runs on its own connection in a daemon thread, so
    it's safe from a request handler or the scheduler thread; never raises, never
    touches the caller's transaction. Backs the `notify` pipeline primitive."""
    def _go():
        conn = get_conn()
        # A bell item so the alarm icon mirrors the native push (link_slug carries a
        # path like "/map" — the bell opens it directly). Committed BEFORE the push so
        # the count rides in the payload and the bell updates live.
        try:
            reviews_svc.create_review_item(conn, None, title[:120], (body or "")[:400], (url or "/"))
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        _send_all(conn, title, body, url=url or "/", tag="jbrain-notify")
    threading.Thread(target=_go, daemon=True).start()


def _send_all(conn, title: str, body: str, url: str = "/shares", tag: str = "jbrain-reviews") -> dict:
    """Send a push to every subscription. Returns {sent, failed, errors}. Logs each
    attempt to stdout (visible in `docker compose logs api`) and prunes dead
    endpoints. Never raises."""
    try:
        priv = get_meta(_PRIV_META)
        if not priv:
            print("[push] no VAPID private key configured", flush=True)
            return {"sent": 0, "failed": 0, "errors": ["no VAPID key"]}
        subs = conn.execute("SELECT id, endpoint, p256dh, auth FROM push_subscriptions").fetchall()
        if not subs:
            return {"sent": 0, "failed": 0, "errors": []}
        count = reviews_svc.pending_count(conn)
        payload = json.dumps({"title": title, "body": body, "count": count,
                              "url": url, "tag": tag})
        try:
            from pywebpush import webpush, WebPushException
        except Exception as exc:
            print(f"[push] pywebpush import failed: {exc!r}", flush=True)
            return {"sent": 0, "failed": len(subs), "errors": [f"pywebpush unavailable: {exc}"]}
        subject = get_settings().vapid_subject
        sent = 0; failed = 0; errors: list[str] = []; dead: list[int] = []
        for s in subs:
            ep = s["endpoint"]
            try:
                r = webpush(
                    subscription_info={"endpoint": ep, "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                    data=payload, vapid_private_key=priv, vapid_claims={"sub": subject}, timeout=10,
                )
                sent += 1
                print(f"[push] OK status={getattr(r, 'status_code', '?')} -> {ep[:64]}", flush=True)
            except WebPushException as exc:
                failed += 1
                code = getattr(getattr(exc, "response", None), "status_code", None)
                msg = f"{code} {str(exc)[:160]}"
                errors.append(msg)
                print(f"[push] FAIL {msg} -> {ep[:64]}", flush=True)
                if code in (404, 410):
                    dead.append(s["id"])
            except Exception as exc:
                failed += 1
                errors.append(str(exc)[:180])
                print(f"[push] ERROR {exc!r} -> {ep[:64]}", flush=True)
        if dead:
            conn.executemany("DELETE FROM push_subscriptions WHERE id = ?", [(i,) for i in dead])
            conn.commit()
        print(f"[push] done: sent={sent} failed={failed}", flush=True)
        return {"sent": sent, "failed": failed, "errors": errors[:5]}
    except Exception as exc:
        print(f"[push] _send_all crashed: {exc!r}", flush=True)
        return {"sent": 0, "failed": 0, "errors": [str(exc)[:180]]}
    except Exception:
        pass   # a notification must never break the request that triggered it
