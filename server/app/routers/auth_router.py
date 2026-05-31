"""Auth endpoints for access-key login.

- GET  /api/auth/info   (public) — brain name, for the key-entry screen.
- GET  /api/auth/verify (key-gated) — confirms a pasted key is valid.
"""
from fastapi import APIRouter

from ..auth import CurrentUser
from ..config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/info")
def info():
    return {"brain_name": get_settings().brain_name}


@router.get("/verify", dependencies=[CurrentUser])
def verify():
    return {"ok": True, "brain_name": get_settings().brain_name}
