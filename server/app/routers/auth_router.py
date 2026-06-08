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
    """Return public brain metadata for the key-entry screen.

    The version is intentionally omitted here (it maps a public deployment to
    specific commits); it is returned only after successful auth via /verify.

    Returns:
        Dict with 'brain_name'.
    """
    # Public: brain name only (for the key-entry screen). The exact version is
    # NOT exposed pre-auth (it maps a public deployment to specific commits).
    return {"brain_name": get_settings().brain_name}


@router.get("/verify", dependencies=[CurrentUser])
def verify():
    """Verify a valid access key and return a full boot configuration snapshot.

    Returns version, LLM availability, timezone, owner state, LLM provider key
    presence, VAPID public key, and an initial capabilities snapshot — so the PWA
    gets a free health check on boot without an extra round-trip.

    Returns:
        Dict with ok, brain_name, version, has_llm, app_tz, owner_set, llm_keys,
        vapid_public_key, and capabilities.
    """
    # Version is returned here (authed) so the PWA can do its compatibility check.
    # has_llm gates LLM-only UI; app_tz lets the UI render UTC timestamps in the
    # owner's configured local zone (labeled).
    from ..services import clock, push, people, system_status
    from ..db import get_conn
    s = get_settings()
    # owner_set drives the first-run onboarding gate: false while the owner is still the
    # unnamed default, so first-person attribution falls back to the placeholder "the owner".
    owner_set = people.owner_name(get_conn()) != "the owner"
    return {"ok": True, "brain_name": s.brain_name, "version": APP_VERSION,
            "has_llm": s.has_llm, "app_tz": clock.app_tz_name(), "owner_set": owner_set,
            # Which provider keys are present, so the model picker can warn before you
            # select a model whose key isn't configured.
            "llm_keys": {"anthropic": s.has_anthropic, "xai": s.has_xai},
            "vapid_public_key": push.public_key(),
            # Same `capabilities` shape as GET /api/system/status, so the PWA gets a
            # FREE initial health snapshot on boot (one client adapter ingests both).
            # NB: this makes /verify run one SELECT 1 — no longer DB-touch-free.
            "capabilities": system_status.capabilities()}
