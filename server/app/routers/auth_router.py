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
    # Public: brain name only (for the key-entry screen). The exact version is
    # NOT exposed pre-auth (it maps a public deployment to specific commits).
    return {"brain_name": get_settings().brain_name}


@router.get("/verify", dependencies=[CurrentUser])
def verify():
    # Version is returned here (authed) so the PWA can do its compatibility check.
    # has_llm gates LLM-only UI; app_tz lets the UI render UTC timestamps in the
    # owner's configured local zone (labeled).
    from ..services import clock, push
    return {"ok": True, "brain_name": get_settings().brain_name, "version": APP_VERSION,
            "has_llm": get_settings().has_llm, "app_tz": clock.app_tz_name(),
            "vapid_public_key": push.public_key()}
