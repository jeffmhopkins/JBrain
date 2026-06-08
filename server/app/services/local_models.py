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

import glob
import json
import os
import threading
import time
import urllib.request
from typing import Iterator

from ..config import get_settings

# Reserve for the OS + JBrain (embeddings/whisper resident) + headroom when sizing
# what a box can run; the rest is "usable" for a model. ~8 GB on a 32 GB box → ~24 GB.
_RAM_RESERVE_BYTES = 8 * 1024 ** 3
# A model whose resident estimate exceeds this fits but is flagged slow on CPU.
_SLOW_RAM_BYTES = 9 * 1024 ** 3

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


def pull_events(name: str) -> Iterator[dict]:
    """Yield the PWA's typed pull events for `name`, mapping Ollama's raw progress.

    Wraps pull_model() and normalises each raw dict to one of: {"type": "status",
    "status"}, {"type": "progress", "completed", "total", "status"}, {"type": "error",
    "message"}, {"type": "done"}. Always ends with a terminal done/error so the client
    can close its progress UI. Errors (including transport failures) become a terminal
    error event rather than propagating.

    Args:
        name: Model id to pull.

    Yields:
        Typed event dicts for SSE serialisation.
    """
    errored = False
    try:
        for evt in pull_model(name):
            if evt.get("error"):
                errored = True
                yield {"type": "error", "message": evt["error"]}
            elif evt.get("total"):
                yield {"type": "progress", "completed": evt.get("completed", 0),
                       "total": evt["total"], "status": evt.get("status", "")}
            elif evt.get("status") == "success":
                yield {"type": "done"}
            else:
                yield {"type": "status", "status": evt.get("status", "")}
    except Exception as exc:  # noqa: BLE001 — surface as a terminal error frame
        yield {"type": "error", "message": str(exc)[:200]}
        return
    if not errored:
        yield {"type": "done"}


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


def delete_model(name: str) -> bool:
    """Delete a pulled model via Ollama's DELETE /api/delete.

    Args:
        name: Model id to remove, e.g. 'qwen2.5:7b'.

    Returns:
        True on success; False if the admin URL is unset or the call failed.
    """
    base = _admin_base()
    if not base or not name:
        return False
    body = json.dumps({"name": name}).encode()
    req = urllib.request.Request(f"{base}/api/delete", data=body,
                                 headers={"Content-Type": "application/json"}, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception:  # noqa: BLE001
        return False


def _total_ram_bytes() -> int:
    """Return total physical RAM in bytes from /proc/meminfo, or 0 if unknown."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:  # noqa: BLE001
        pass
    return 0


def hardware() -> dict:
    """Return a coarse hardware profile for sizing what models the box can run.

    Returns:
        Dict with usable_ram_bytes (total minus an OS/JBrain reserve), total_ram_bytes,
        cpu_only (no GPU device detected), and a human note.
    """
    total = _total_ram_bytes()
    usable = max(total - _RAM_RESERVE_BYTES, total // 2) if total else 0
    cpu_only = not (glob.glob("/dev/nvidia[0-9]*") or os.path.exists("/dev/kfd"))
    note = ("CPU-only — generation speed is bound by RAM bandwidth; "
            "prefer a 7-8B model." if cpu_only else "GPU detected.")
    return {"usable_ram_bytes": usable, "total_ram_bytes": total,
            "cpu_only": cpu_only, "note": note}


def ram_estimate(size_bytes: int) -> int:
    """Estimate a model's resident RAM from its on-disk size (weights + KV headroom).

    Args:
        size_bytes: Model size on disk (from /api/tags).

    Returns:
        Estimated resident bytes.
    """
    return int(size_bytes * 1.3)


def describe_models() -> dict:
    """Build the local-models view for the settings UI: installed models + hardware.

    Each installed model carries a server-computed fit verdict (against usable RAM) and
    a 'slow on CPU' warning, so the UI never hardcodes thresholds.

    Returns:
        Dict with running (Ollama reachable), models (list), and hardware.
    """
    models = list_models()
    running = _admin_reachable(models)
    hw = hardware()
    usable = hw["usable_ram_bytes"]
    cfg_model = get_settings().llm_local_model
    out = []
    for m in models:
        est = ram_estimate(m.get("size", 0) or 0)
        fits = (usable == 0) or (est <= usable)     # unknown RAM → don't false-negative
        warn = "Large model — slow on CPU" if (fits and est > _SLOW_RAM_BYTES) else None
        out.append({
            "name": m["name"],
            "size_bytes": m.get("size", 0) or 0,
            "ram_estimate_bytes": est,
            "fits": fits,
            "warn": warn,
            # The configured model carries live readiness; others are simply present.
            "state": readiness()["state"] if m["name"] == cfg_model else "ready",
        })
    return {"running": running, "models": out, "hardware": hw}
