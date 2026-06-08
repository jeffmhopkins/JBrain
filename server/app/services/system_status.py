"""Aggregated server/API capability snapshot for the health indicator.

ONE cheap document: in-memory readiness reads (embeddings/audio) + config flags +
exactly one `SELECT 1` (the only per-poll query — it catches a locked WAL /
read-only mount that process-liveness misses). NO model load, NO network, NO LLM
calls, NO tokens.

LLM "usable" is the SINGLE authoritative predicate `llm.has_credentials()` (the
ACTIVE provider's creds) — the same predicate the share landing (share.llm_ready)
and every feature gate use. The per-provider `providers` map is INFORMATIONAL ONLY
(ModelPicker's per-provider hint); it does NOT define usability, because the active
provider may differ from which keys merely exist.
"""
from __future__ import annotations


def _safe(fn) -> dict:
    """Call a zero-argument readiness probe and swallow any exception.

    A probe that genuinely raises (e.g. a broken DB making a get_meta call throw)
    reports ``{'state': 'failed', 'last_error': ...}`` rather than propagating —
    'failed' (not 'unknown') because the read actually errored; 'unknown' is reserved
    for pre-observation. The health doc is always served.

    Args:
        fn: Zero-argument callable that returns a readiness dict.

    Returns:
        The probe's dict, or {'state': 'failed', 'last_error': ...} on exception.
    """
    try:
        return fn()
    except Exception as exc:                                   # noqa: BLE001
        return {"state": "failed", "last_error": str(exc)[:200]}


def capabilities() -> dict:
    """Return the aggregated server capability snapshot for the health indicator.

    Reads in-memory readiness state for all subsystems (embeddings, transcription,
    push, geocoder) plus a single ``SELECT 1`` for the DB. No model loads, no network
    calls, no LLM tokens are consumed.

    Returns:
        Dict with per-subsystem readiness dicts keyed by: llm, local_llm, embeddings,
        transcription, push, geocoder, db. Each value has at least a 'state' key
        ('ready', 'absent', or 'failed').
    """
    from . import embeddings, audio_transcription, push, geocode, llm, local_models
    from ..config import get_settings
    from ..db import get_conn
    s = get_settings()

    def _db():
        """Probe the DB with SELECT 1; raises on failure."""
        get_conn().execute("SELECT 1").fetchone()
        return {"state": "ready"}

    def _db_safe():
        """Wrap _db() to never propagate exceptions."""
        try:
            return _db()
        except Exception as exc:                               # noqa: BLE001
            return {"state": "failed", "last_error": str(exc)[:200]}

    def _llm():
        """Return the authoritative LLM readiness dict."""
        return {
            # AUTHORITATIVE: active-provider credentials (same predicate as share.llm_ready()
            # and every feature gate). NOT s.has_anthropic/has_xai — presence != usable.
            "state": "ready" if llm.has_credentials() else "absent",
            "verified": None,                                  # never live-checked (cost)
            # INFORMATIONAL ONLY — per-provider key presence for ModelPicker's hint.
            "providers": {"anthropic": s.has_anthropic, "xai": s.has_xai, "local": s.has_local},
        }

    return {
        "llm":           _safe(_llm),
        # Local LLM is INFORMATIONAL (like embeddings): a per-tier offload, not the
        # authoritative `llm` predicate above. Returns 'absent' on default installs.
        "local_llm":     _safe(local_models.readiness),
        "embeddings":    _safe(embeddings.readiness),
        "transcription": _safe(audio_transcription.readiness),
        "push":          _safe(lambda: {"state": "ready" if push.public_key() else "absent"}),
        "geocoder":      _safe(lambda: {"state": "ready" if geocode.enabled() else "absent"}),
        "db":            _db_safe(),
    }
