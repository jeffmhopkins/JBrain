"""Deferred, coalesced entity-index rebuilds.

The identity controls (merge / split / alias) on the Entities page must (a) record a
DURABLE decision synchronously — never lost — and (b) NOT block the owner's click on a
full entity_index.rebuild(), which runs _sync_embeddings (fastembed: networked/slow).

So the request commits the decision, then calls request_rebuild(): it flips a DURABLE
'dirty' flag (committed on its own, so a crash before the rebuild is reconciled on the
next boot / by the scheduler) and kicks a single background worker thread to run the
actual rebuild. This mirrors services/image_analysis.py: a daemon thread with its OWN
thread-local DB connection (sqlite connections are per-thread — a background rebuild must
never reuse the request's), a status flag the UI polls, and reset_on_boot() at startup.

COALESCING: at most one worker runs at a time. A request arriving while a rebuild is in
flight sets a 'pending' marker; the running worker loops once more when it finishes, so a
burst of rapid clicks collapses to (at most) one extra pass that reflects the LAST decision
— never a stampede of concurrent rebuilds, never a lost decision. The durable dirty flag is
cleared at the START of each pass (under the lock), so a decision committed DURING a rebuild
re-dirties and is guaranteed a fresh pass.

FAILURE: a rebuild raising leaves the durable decision intact AND the dirty flag set, so the
scheduler backstop (or the next decision) reconciles. The worker never crashes the process.
"""
from __future__ import annotations

import logging
import threading

from ..db import get_conn, get_meta, set_meta

log = logging.getLogger("jbrain")

# meta keys (durable, survive restart).
_DIRTY = "entities:rebuild_dirty"              # "1" while a rebuild is owed
_STATUS = "entities:rebuild_status"            # idle | running | error
_GENERATION = "entities:rebuild_generation"    # bumped after each SUCCESSFUL pass (UI completion signal)
_LAST_ERROR = "entities:rebuild_error"         # last failure detail, for diagnostics

# Coalescing state (process-local). _lock guards all of it AND the dirty/pending handoff.
_lock = threading.Lock()
_running = False     # a worker thread is alive
_pending = False     # a rebuild was requested while one was running → run once more

# Test seam: when True, request_rebuild runs the rebuild INLINE (no thread) so a TestClient
# request observes the reconciled state synchronously. Left False in production.
run_inline = False


def _set_status(conn, st: str, detail: str | None = None) -> None:
    set_meta(conn, _STATUS, st)
    if detail is not None:
        set_meta(conn, _LAST_ERROR, detail[:500])
    conn.commit()


def status(conn) -> dict:
    """The UI poll target: is a rebuild owed/in-flight, the completion generation, last error."""
    st = get_meta(_STATUS, "idle", conn=conn) or "idle"
    dirty = (get_meta(_DIRTY, "0", conn=conn) or "0") == "1"
    gen = int(get_meta(_GENERATION, "0", conn=conn) or "0")
    err = get_meta(_LAST_ERROR, None, conn=conn)
    # "rebuilding" = the index isn't yet reconciled with the durable decisions: either a
    # worker is running, or one is owed (dirty) and about to run.
    return {"rebuilding": dirty or st == "running", "status": st, "generation": gen,
            "last_error": err if st == "error" else None}


def _rebuild_pass(conn) -> None:
    """One rebuild pass. Clears the dirty flag FIRST (under the lock) so a decision committed
    mid-rebuild re-dirties and earns a fresh pass, then runs the (slow) entity_index.rebuild."""
    from . import entity_index
    with _lock:
        set_meta(conn, _DIRTY, "0")          # claim the owed work for THIS pass
        conn.commit()
    entity_index.rebuild(conn)               # commits internally
    gen = int(get_meta(_GENERATION, "0", conn=conn) or "0") + 1
    set_meta(conn, _GENERATION, str(gen))
    set_meta(conn, _STATUS, "idle")
    set_meta(conn, _LAST_ERROR, "")
    conn.commit()


def _worker() -> None:
    """Background daemon: own thread-local connection. Drains the coalesced rebuild queue —
    runs a pass, and if another request landed meanwhile (_pending), runs once more."""
    global _running, _pending
    conn = get_conn()
    try:
        while True:
            try:
                _set_status(conn, "running")
                _rebuild_pass(conn)
            except Exception as exc:  # noqa: BLE001 — never let the worker die; the decision is durable
                log.warning("entity_rebuild pass failed: %s", exc)
                try:
                    # Re-dirty so the scheduler backstop / next decision reconciles; surface the error.
                    set_meta(conn, _DIRTY, "1")
                    _set_status(conn, "error", str(exc))
                except Exception:  # noqa: BLE001
                    pass
            with _lock:
                if not _pending:
                    _running = False
                    return
                _pending = False        # consume the coalesced request and loop for one more pass
    except Exception:  # noqa: BLE001 — absolute backstop so _running can't get stuck True
        with _lock:
            _running = False


def request_rebuild(conn) -> None:
    """Request thread (called AFTER the decision is committed): mark the index dirty durably,
    then ensure exactly one worker is/gets running (coalescing concurrent requests)."""
    global _running, _pending
    set_meta(conn, _DIRTY, "1")
    conn.commit()                       # durable BEFORE we hand off — survives a crash here

    if run_inline:                      # test seam: reconcile synchronously
        try:
            _set_status(conn, "running")
            _rebuild_pass(conn)
        except Exception as exc:  # noqa: BLE001
            set_meta(conn, _DIRTY, "1")
            _set_status(conn, "error", str(exc))
        return

    with _lock:
        if _running:
            _pending = True             # a worker is live → it will run once more for us
            return
        _running = True
    threading.Thread(target=_worker, daemon=True, name="entity-rebuild").start()


def reset_on_boot(conn) -> None:
    """On boot, clear any stale 'running' status from a prior process and re-trigger a rebuild
    if one was owed (dirty) — so a decision whose rebuild was interrupted by a restart is
    reconciled. Idempotent; safe to call before the app serves traffic."""
    st = get_meta(_STATUS, "idle", conn=conn) or "idle"
    if st == "running":
        set_meta(conn, _STATUS, "idle")
        conn.commit()
    if (get_meta(_DIRTY, "0", conn=conn) or "0") == "1":
        request_rebuild(conn)
