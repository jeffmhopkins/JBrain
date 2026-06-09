"""Enforce the SQLite connection-ownership invariant so it can't silently regress.

Every recurring "Edit with AI / chat / research stops responding" hang traced to ONE root
cause: the shared event-loop connection (get_conn() is thread-local, and the event loop is a
single thread, so every concurrent async turn gets the SAME connection) was handed to a worker
thread via asyncio.to_thread. Two turns then drove one sqlite3.Connection at once and wedged it
on the connection mutex — an unkillable hang the surrounding try/except can't catch.

These tests turn that invariant from honor-code into something enforced:

1. ``test_connection_is_thread_affine`` — production connections are opened
   ``check_same_thread=True``, so a cross-thread use RAISES (loudly, at the offending call)
   instead of deadlocking. This fails if anyone flips the flag back.
2. ``test_no_async_offload_passes_the_event_loop_connection`` — a static guard: no
   ``asyncio.to_thread`` / ``run_in_executor`` call site in ``app/`` may take a bare
   ``conn``/``tconn`` argument. Offloaded DB work must open its own connection inside the
   worker closure (the ``_persist_user_turn`` pattern). This fails on the exact code shape that
   caused the gather/research hangs.
"""
import os
import re
import sqlite3
import threading
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_connection_is_thread_affine(tmp_path):
    """A production connection refuses use from a thread other than its creator.

    This is the runtime guard: the cross-thread share that caused the hangs now raises a
    ``sqlite3.ProgrammingError`` at the first DB call instead of wedging the connection mutex.
    """
    os.environ["DB_PATH"] = str(tmp_path / "affinity.db")
    from app.config import get_settings
    get_settings.cache_clear()
    import app.db as db

    conn = db._connect()
    captured: dict = {}

    def _use_from_another_thread():
        """Touch the connection from a foreign thread; record whatever it raises."""
        try:
            conn.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001 — we WANT to capture the guard's raise
            captured["exc"] = exc

    try:
        t = threading.Thread(target=_use_from_another_thread)
        t.start()
        t.join(5)
        assert isinstance(captured.get("exc"), sqlite3.ProgrammingError), (
            "connections must be thread-affine (check_same_thread=True) so a cross-thread use "
            "raises immediately instead of silently wedging the connection mutex"
        )
    finally:
        conn.close()


# asyncio.to_thread(fn, conn, …) / loop.run_in_executor(ex, fn, conn, …) — a bare conn/tconn
# anywhere in the call's argument list is the forbidden shape. DOTALL so a multi-line call is
# caught too. `\bconn\b` does NOT match get_conn/_thread_conn (the '_' is a word char), so the
# sanctioned "open get_conn() inside the worker closure" form passes cleanly.
_OFFLOAD_CALL = re.compile(r"(?:asyncio\.to_thread|run_in_executor)\((.*?)\)", re.DOTALL)
_BARE_CONN = re.compile(r"\b(?:conn|tconn)\b")


def test_no_async_offload_passes_the_event_loop_connection():
    """No offload site may hand a worker the shared event-loop connection.

    Offloaded DB work must open its OWN connection inside the worker closure (get_conn() is
    thread-local) rather than reach across the await onto the loop's connection. This is the
    code-shape that produced the gather "Looking through your notes…" and research
    "stops responding" hangs.
    """
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        text = path.read_text()
        for m in _OFFLOAD_CALL.finditer(text):
            if _BARE_CONN.search(m.group(1)):
                line_no = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(APP_DIR.parent)}:{line_no}")
    assert not offenders, (
        "these offload sites pass the shared event-loop connection into a worker thread — open a "
        "worker-local connection inside the closure instead (see _persist_user_turn):\n  "
        + "\n  ".join(offenders)
    )
