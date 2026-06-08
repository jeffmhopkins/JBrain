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
    """Return the SHA-256 hex digest of ``key``.

    Args:
        key: Raw access-key string to hash.

    Returns:
        Lowercase hexadecimal SHA-256 digest.
    """
    return hashlib.sha256(key.encode()).hexdigest()


def ensure_access_key() -> str | None:
    """Seed or rotate the access key at startup.

    If a key is configured in the environment it is authoritative: its hash
    is (re)written to the database, enabling rotation by editing ``.env``.
    If no key is configured and none exists yet, a 256-bit key is generated,
    its hash is stored, and the raw key is returned for first-run display.

    Returns:
        The newly generated raw key string if one was created, otherwise None.
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
    """Verify a raw access key against the stored hash in constant time.

    Args:
        key: Raw key presented by the client; may be None.

    Returns:
        True if the key matches the stored hash, False otherwise.
    """
    if not key:
        return False
    stored = get_meta(_HASH_KEY)
    if not stored:
        return False
    return hmac.compare_digest(_hash(key), stored)


def _extract_key(request: Request) -> str | None:
    """Extract the raw access key from the request headers.

    Looks for ``Authorization: Bearer <key>`` first, then the
    ``X-JBrain-Key`` header.

    Args:
        request: Incoming FastAPI/Starlette request.

    Returns:
        The raw key string, or None if neither header is present.
    """
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
    """Resolve the client IP address from the request.

    Prefers the first entry of ``X-Forwarded-For`` (set by a reverse proxy)
    over the direct connection address.

    Args:
        request: Incoming FastAPI/Starlette request.

    Returns:
        The client IP string, or ``"?"`` when it cannot be determined.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def require_key(request: Request) -> str:
    """Gate a route on a valid access key; raise 401/429 on failure.

    Applies a per-IP in-memory throttle to limit failed authentication
    attempts. Resets the failure counter for the IP on success.

    Args:
        request: Incoming FastAPI/Starlette request.

    Returns:
        The string ``"client"`` on successful authentication.

    Raises:
        HTTPException: 429 if the IP has exceeded the failure threshold within
            the window, or 401 if the key is invalid or missing.
    """
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
    """Dependency for location-ingest endpoints.

    Authorises either the full access key (returns None; source is taken from
    the request body) or a per-person location key (returns that person row so
    the caller can force the fix's source to that person). A location key
    grants only location ingest and dictation capture — it cannot read the
    location trail or reach any other route.

    Args:
        request: Incoming FastAPI/Starlette request.

    Returns:
        None if authenticated with the full access key, or the ``people``
        row matched by a per-person location key.

    Raises:
        HTTPException: 429 on throttle excess, or 401 on invalid/missing key.
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
    """Dependency for device dictation capture (watch note relayed by the phone).

    Authorises either the full access key (returns None) or a per-person
    location key (returns the person row so the note can be attributed to
    them). This lets a family phone configured with only a scoped key drop a
    dictated note without holding the master key. Validation and failure
    throttling are identical to ``require_location_writer``.

    Args:
        request: Incoming FastAPI/Starlette request.

    Returns:
        None if authenticated with the full access key, or the ``people``
        row matched by a per-person location key.

    Raises:
        HTTPException: 429 on throttle excess, or 401 on invalid/missing key.
    """
    return require_location_writer(request)


CaptureWriter = Depends(require_capture_writer)
