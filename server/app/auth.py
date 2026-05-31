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


def require_key(request: Request) -> str:
    """Dependency: gate a route on a valid access key."""
    key = _extract_key(request)
    if not verify_key(key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing access key"
        )
    return "client"


# Kept as the name routers already import, now backed by access-key auth.
CurrentUser = Depends(require_key)
