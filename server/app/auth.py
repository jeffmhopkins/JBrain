"""Access-key authentication.

There is a single shared, high-entropy access key (the pasted "cert"). Clients
(PWA and watch) send it as `Authorization: Bearer <key>` (or `X-JBrain-Key`) on
every API call over HTTPS. Only the SHA-256 hash of the key is stored on the
server; validation is a constant-time compare. The key has ~256 bits of entropy,
so a fast hash is appropriate (no slow KDF needed) and lets us auth per request
cheaply.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import Depends, HTTPException, Request, status

from .config import get_settings
from .db import get_conn, get_meta, set_meta

_HASH_KEY = "access_key_hash"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def ensure_access_key() -> str | None:
    """Seed/rotate the access key at startup.

    - If an access key is configured in the environment, it is authoritative:
      its hash is (re)written, supporting rotation by editing .env.
    - Otherwise, if no key exists yet, generate one, store its hash, and return
      the raw key so first-run setup can reveal it. Returns None when nothing
      new needs revealing.
    """
    conn = get_conn()
    configured = get_settings().jbrain_access_key.strip()

    if configured:
        if len(configured) < 24:
            print("[auth] WARNING: JBRAIN_ACCESS_KEY is short; use a long random "
                  "key (the installer generates a 256-bit one).", flush=True)
        set_meta(conn, _HASH_KEY, _hash(configured))
        conn.commit()
        return None

    if get_meta(_HASH_KEY) is None:
        generated = secrets.token_urlsafe(32)
        set_meta(conn, _HASH_KEY, _hash(generated))
        conn.commit()
        return generated

    return None


def verify_key(key: str | None) -> bool:
    if not key:
        return False
    stored = get_meta(_HASH_KEY)
    if not stored:
        return False
    return hmac.compare_digest(_hash(key), stored)


def _extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-jbrain-key")


# Lightweight in-memory per-IP throttle on failed auth (defense-in-depth against
# online guessing of a weak operator-chosen key; a generated 256-bit key is
# uncrackable anyway). Best-effort, bounded; resets on success.
_FAIL_WINDOW = 60.0
_FAIL_MAX = 30
_fails: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def require_key(request: Request) -> str:
    """Dependency: gate a route on a valid access key."""
    ip = _client_ip(request)
    now = time.monotonic()
    recent = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW]
    if len(recent) >= _FAIL_MAX:
        _fails[ip] = recent
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many attempts; slow down.")
    if not verify_key(_extract_key(request)):
        recent.append(now)
        _fails[ip] = recent
        if len(_fails) > 10_000:  # bound memory
            _fails.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing access key"
        )
    _fails.pop(ip, None)  # success clears the IP's failure history
    return "client"


# Kept as the name routers already import, now backed by access-key auth.
CurrentUser = Depends(require_key)


def require_location_writer(request: Request):
    """Dependency for the location-INGEST endpoints. Authorizes EITHER:
      - the full access key  → returns None (source taken from the request body), or
      - a per-person LOCATION KEY → returns that person row (the caller forces the
        fix's source to this person).
    A location key grants ONLY this; it can't read the trail or reach any other route.
    """
    ip = _client_ip(request)
    now = time.monotonic()
    recent = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW]
    if len(recent) >= _FAIL_MAX:
        _fails[ip] = recent
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many attempts; slow down.")
    key = _extract_key(request)
    if verify_key(key):
        _fails.pop(ip, None)
        return None
    if key:
        row = get_conn().execute(
            "SELECT * FROM people WHERE location_key = ?", (key,)
        ).fetchone()
        if row is not None:
            _fails.pop(ip, None)
            return row
    recent.append(now)
    _fails[ip] = recent
    if len(_fails) > 10_000:
        _fails.clear()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid or missing access key")


LocationWriter = Depends(require_location_writer)


def require_capture_writer(request: Request):
    """Dependency for device DICTATION capture (a watch note relayed by the phone).
    Authorizes EITHER:
      - the full access key  → returns None, or
      - a per-person LOCATION KEY → returns that person row, so the note can be
        attributed to them.
    This lets a family phone configured with only its scoped setup-code key drop a
    dictated note, without ever putting the master key on the device. The validation
    (and failure throttle) is identical to the location writer.
    """
    return require_location_writer(request)


CaptureWriter = Depends(require_capture_writer)
