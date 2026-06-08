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
    """Never let one subsystem's read 500 the whole health doc. A read that
    genuinely RAISES (e.g. a broken DB making a get_meta call throw) reports
    'failed' + the error — 'failed' (not 'unknown') because the read actually
    errored; 'unknown' is reserved for pre-observation. The dot still shows the
    fault rather than a benign 'checking…', and the doc is always served."""
    try:
        return fn()
    except Exception as exc:                                   # noqa: BLE001
        return {"state": "failed", "last_error": str(exc)[:200]}


def capabilities() -> dict:
    from . import embeddings, audio_transcription, push, geocode, llm
    from ..config import get_settings
    from ..db import get_conn
    s = get_settings()

    def _db():
        get_conn().execute("SELECT 1").fetchone()
        return {"state": "ready"}

    def _db_safe():
        try:
            return _db()
        except Exception as exc:                               # noqa: BLE001
            return {"state": "failed", "last_error": str(exc)[:200]}

    def _llm():
        return {
            # AUTHORITATIVE: active-provider credentials (same predicate as share.llm_ready()
            # and every feature gate). NOT s.has_anthropic/has_xai — presence != usable.
            "state": "ready" if llm.has_credentials() else "absent",
            "verified": None,                                  # never live-checked (cost)
            # INFORMATIONAL ONLY — per-provider key presence for ModelPicker's hint.
            "providers": {"anthropic": s.has_anthropic, "xai": s.has_xai},
        }

    return {
        "llm":           _safe(_llm),
        "embeddings":    _safe(embeddings.readiness),
        "transcription": _safe(audio_transcription.readiness),
        "push":          _safe(lambda: {"state": "ready" if push.public_key() else "absent"}),
        "geocoder":      _safe(lambda: {"state": "ready" if geocode.enabled() else "absent"}),
        "db":            _db_safe(),
    }
