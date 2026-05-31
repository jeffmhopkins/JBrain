"""Auth endpoints for access-key login.

- GET  /api/auth/info   (public) — brain name, for the key-entry screen.
- GET  /api/auth/verify (key-gated) — confirms a pasted key is valid.
"""
from fastapi import APIRouter

from ..auth import CurrentUser
from ..config import get_settings
from ..version import APP_VERSION

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/info")
def info():
    # Public: brain name + server version so the PWA can verify compatibility
    # before/at connect (works cross-origin for a separately-hosted PWA).
    return {"brain_name": get_settings().brain_name, "version": APP_VERSION}


@router.get("/verify", dependencies=[CurrentUser])
def verify():
    return {"ok": True, "brain_name": get_settings().brain_name}
