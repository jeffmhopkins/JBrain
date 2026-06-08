"""Ollama management API client + local-model readiness state.

Talks to the Ollama HOST API (not the OpenAI-compat /v1 path used for generation):
GET /api/tags to list pulled models, POST /api/pull to download one (streamed
NDJSON progress). Tracks a process-level readiness state for the configured local
model, reusing the embeddings readiness pattern, so the health indicator can show
local warm-up without a model load or token spend on every poll.

Uses stdlib urllib (no heavy dependency) — the same ethos as system_status: the
health surface stays cheap. readiness() is an O(1) in-memory read; only warm() /
list_models() / pull_model() touch the network, and never at import time or on the
per-poll capabilities() path.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Iterator

from ..config import get_settings

# --- readiness state (cheap, in-memory; no I/O on read) ----------------------
# LOUD CONSTRAINT: this is PER-PROCESS state, exactly like embeddings._state. JBrain
# runs a SINGLE uvicorn worker (server/Dockerfile, no --workers). Do NOT add --workers
# without a shared readiness store or the health dot will flicker between workers.
_state = "unknown"            # unknown | absent | unavailable | pulling | warming | ready | failed
_last_error: str | None = None
_model: str | None = None
_state_since = time.time()
_state_lock = threading.Lock()


def _set_state(s: str, *, err: str | None = None, model: str | None = None) -> None:
    """Update the process-level local-model readiness state under the state lock.

    Args:
        s: New state string (unknown/absent/unavailable/pulling/warming/ready/failed).
        err: Optional error message; truncated to 200 chars.
        model: Optional model id this state refers to.
    """
    global _state, _last_error, _model, _state_since
    with _state_lock:
        _state = s
        _last_error = err and str(err)[:200]
        if model is not None:
            _model = model
        _state_since = time.time()


def readiness() -> dict:
    """Return the in-memory local-model readiness snapshot. O(1), no I/O, never blocks.

    Safe to call on every health poll.

    Returns:
        Dict with keys: state, last_error, model, since.
    """
    with _state_lock:                       # one lock → the snapshot is never torn
        return {"state": _state, "last_error": _last_error, "model": _model, "since": _state_since}


def _admin_base() -> str:
    """Return the Ollama admin base URL (no trailing slash)."""
    return (get_settings().llm_local_admin_url or "").rstrip("/")


def list_models() -> list[dict]:
    """Return locally-pulled models from GET /api/tags, or [] when unreachable.

    Returns:
        List of {"name", "size"} dicts (empty if Ollama is down or returns nothing).
    """
    base = _admin_base()
    if not base:
        return []
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode() or "{}")
    except Exception:  # noqa: BLE001 — unreachable / bad JSON → no models
        return []
    return [{"name": m.get("name", ""), "size": m.get("size", 0)}
            for m in (data.get("models") or []) if m.get("name")]


def model_present(name: str) -> bool:
    """Return True if `name` (exact, or base before the ':tag') is pulled locally.

    Args:
        name: Model id to check, e.g. 'qwen2.5:7b'.

    Returns:
        True if the model (or its base name) appears in list_models().
    """
    if not name:
        return False
    base = name.split(":", 1)[0]
    names = [m["name"] for m in list_models()]
    return any(n == name or n.split(":", 1)[0] == base for n in names)


def pull_model(name: str) -> Iterator[dict]:
    """Stream POST /api/pull for `name`, yielding Ollama's NDJSON progress dicts.

    Sets readiness to 'pulling' as it streams and 'ready'/'failed' at the end. Pulls are
    resumable in Ollama's content-addressed store, so a restart mid-download continues.

    Args:
        name: Model id to pull, e.g. 'qwen2.5:7b'.

    Yields:
        Progress dicts, e.g. {"status": "downloading", "completed": N, "total": M}.

    Raises:
        RuntimeError: If the admin URL is not configured.
    """
    base = _admin_base()
    if not base:
        raise RuntimeError("local admin URL not configured")
    _set_state("pulling", model=name)
    body = json.dumps({"name": name, "stream": True}).encode()
    req = urllib.request.Request(f"{base}/api/pull", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    errored = False
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                if evt.get("error"):
                    errored = True
                    _set_state("failed", err=evt["error"], model=name)
                yield evt
    except Exception as exc:  # noqa: BLE001 — surface as a failed state, re-raise for the caller
        _set_state("failed", err=str(exc), model=name)
        raise
    if not errored:                          # don't clobber a mid-stream error with 'ready'
        _set_state("ready", model=name)


def _timeout() -> float:
    """Return a generous timeout for pulls (multi-GB downloads can run long)."""
    # A pull is not a request the user is blocking on; allow it to run well past the
    # per-generation cap. Capped so a wedged connection still eventually errors.
    try:
        return max(float(get_settings().llm_timeout_seconds), 3600.0)
    except Exception:  # noqa: BLE001
        return 3600.0


def warm() -> dict:
    """Probe local-model readiness once and update the in-memory state.

    Maps: local disabled → 'absent'; Ollama unreachable → 'unavailable'; configured
    model missing → 'pulling' (a background pull is expected to be in flight); model
    present → 'ready'. Probe error → 'failed'. Called from the boot warm task and the
    admin route, never on the per-poll health path.

    Returns:
        The readiness() snapshot after the probe.
    """
    s = get_settings()
    if not s.has_local:
        _set_state("absent", model=s.llm_local_model or None)
        return readiness()
    model = s.llm_local_model
    try:
        models = list_models()
        if not _admin_reachable(models):
            _set_state("unavailable", err="ollama not reachable", model=model or None)
        elif model and not _name_in(model, models):
            _set_state("pulling", model=model)
        else:
            _set_state("ready", model=model or None)
    except Exception as exc:  # noqa: BLE001
        _set_state("failed", err=str(exc), model=model or None)
    return readiness()


def _admin_reachable(models: list[dict]) -> bool:
    """Return True if the Ollama admin endpoint answered (even with zero models).

    list_models() returns [] both for 'unreachable' and 'reachable but empty', so this
    re-probes /api/tags directly to tell the two apart for the readiness state.

    Args:
        models: The list_models() result (used as a fast positive shortcut).

    Returns:
        True if Ollama responded.
    """
    if models:
        return True
    base = _admin_base()
    if not base:
        return False
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _name_in(name: str, models: list[dict]) -> bool:
    """Return True if `name` (or its base) is present in a list_models() result.

    Args:
        name: Model id to look for.
        models: The list_models() result.

    Returns:
        True if present.
    """
    base = name.split(":", 1)[0]
    return any(m["name"] == name or m["name"].split(":", 1)[0] == base for m in models)
