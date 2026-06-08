"""Short-lived signed tokens for direct media streaming.

Lets <img>/<audio>/<video> load an attachment from a SAME-ORIGIN URL the element
can fetch on its own — no Authorization header (media elements can't set one), no
`blob:` URL, and therefore no dependency on a `media-src blob:` CSP (the source is
'self', already allowed by default-src). The token is an HMAC over the attachment
id + an expiry, signed with a per-install secret kept in the `meta` KV (mirrors the
VAPID/access-key seeding). Streaming + range requests live in the router.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from ..db import get_meta, set_meta

_SECRET_META = "media_sign_secret"
_DEFAULT_TTL = 6 * 3600   # a media link is good for one viewing session


def _secret(conn) -> str:
    """Return (and lazily create) the per-install HMAC signing secret from the meta KV store.

    Args:
        conn: Database connection.

    Returns:
        The signing secret string.
    """
    s = get_meta(_SECRET_META, conn=conn)
    if not s:
        s = secrets.token_urlsafe(32)
        set_meta(conn, _SECRET_META, s)
        conn.commit()
    return s


def _sig(secret: str, att_id: int, exp: int) -> str:
    """Compute an HMAC-SHA256 signature over attachment id and expiry.

    Args:
        secret: The per-install signing secret.
        att_id: The attachment id being signed.
        exp: Unix timestamp of the token's expiry.

    Returns:
        Hex-encoded HMAC-SHA256 digest string.
    """
    return hmac.new(secret.encode(), f"{att_id}.{exp}".encode(), hashlib.sha256).hexdigest()


def make_token(conn, att_id: int, ttl: int = _DEFAULT_TTL) -> str:
    """Generate a short-lived signed token for direct media streaming.

    The token is an HMAC over the attachment id and an expiry timestamp, signed with
    the per-install secret. Media elements use it as a same-origin URL parameter —
    no Authorization header required.

    Args:
        conn: Database connection.
        att_id: The attachment id to authorize access to.
        ttl: Token lifetime in seconds (default: 6 hours).

    Returns:
        Token string in the form '{exp}.{sig}'.
    """
    exp = int(time.time()) + ttl
    return f"{exp}.{_sig(_secret(conn), att_id, exp)}"


def verify_token(conn, att_id: int, token: str | None) -> bool:
    """Verify a media streaming token for a given attachment id.

    Checks structure, expiry, and HMAC validity using a constant-time comparison.

    Args:
        conn: Database connection.
        att_id: The attachment id the token must authorize.
        token: Token string from the request, or None.

    Returns:
        True if the token is valid and unexpired, False otherwise.
    """
    if not token or "." not in token:
        return False
    exp_s, _, sig = token.partition(".")
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(sig, _sig(_secret(conn), att_id, exp))
