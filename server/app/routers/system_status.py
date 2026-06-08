"""Soft-auth server/API health endpoint for the PWA's real-time status indicator.

Two INDEPENDENT response builders:
  - public skeleton `{ok, brain, ts}` — returned with NO/invalid key; leaks no more
    than /api/health + /api/auth/info (a reachability probe usable pre-login).
  - full authed document — the capabilities snapshot, only with a valid key.

This is SOFT auth (no router-level CurrentUser): a poll with a missing/rotated key
still answers (with the skeleton) so the client can tell "server reachable" apart
from "needs re-auth" without ever being logged out.

NOTE: this prefix is DELIBERATELY shared with the owner-gated routers/system.py
(which DOES hard-401), but with a different (soft) auth posture. The two never
collide — system.py does not define /status — and registration order is irrelevant
(distinct paths).
"""
from fastapi import APIRouter, Request

from ..auth import _extract_key, verify_key
from ..config import get_settings
from ..services import clock, system_status
from ..version import APP_VERSION

router = APIRouter(prefix="/api/system", tags=["system-status"])


def _public_skeleton() -> dict:                       # builder #1 — EXACT allowlist
    # clock.now_utc() returns an aware UTC datetime; .isoformat() -> "...+00:00".
    # (There is NO clock.iso_now().)
    return {"ok": True, "brain": get_settings().brain_name, "ts": clock.now_utc().isoformat()}


@router.get("/status")
def status(request: Request):
    if not verify_key(_extract_key(request)):         # None/empty/bad → False, never throws
        return _public_skeleton()                     # liveness only; no rollup, no names
    return {**_public_skeleton(), "version": APP_VERSION,
            "capabilities": system_status.capabilities()}   # builder #2 — full (authed)
