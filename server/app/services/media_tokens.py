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
    s = get_meta(_SECRET_META, conn=conn)
    if not s:
        s = secrets.token_urlsafe(32)
        set_meta(conn, _SECRET_META, s)
        conn.commit()
    return s


def _sig(secret: str, att_id: int, exp: int) -> str:
    return hmac.new(secret.encode(), f"{att_id}.{exp}".encode(), hashlib.sha256).hexdigest()


def make_token(conn, att_id: int, ttl: int = _DEFAULT_TTL) -> str:
    exp = int(time.time()) + ttl
    return f"{exp}.{_sig(_secret(conn), att_id, exp)}"


def verify_token(conn, att_id: int, token: str | None) -> bool:
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
