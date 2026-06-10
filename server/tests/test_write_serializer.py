"""Regression: submit_write() serialises every write through one connection, contention-free.

This is the write-serialisation layer behind the "database is locked" / long-held-lock class of
bug. SQLite allows only one writer, and JBrain hits the DB from many threads at once (async turns,
batch jobs, boot warmers, worker threads). submit_write() funnels writes onto a single dedicated
worker connection so writers can NEVER contend for the write lock, the result of a write (e.g.
``lastrowid``) flows back through a Future, and a nested write doesn't deadlock the single worker.

These run in the concurrency tier (real threads, real WAL) — excluded from the default ``./jt``
gate, run via ``./jt back -m concurrency``.
"""
import os
import tempfile
import threading
from concurrent.futures import wait

import pytest

pytestmark = pytest.mark.concurrency


@pytest.fixture()
def fresh_db():
    """Initialise a fresh on-disk DB + a scratch table, and yield the db module.

    On-disk (not :memory:) so the single writer thread's own connection sees the same database the
    test thread created the scratch table in. Resets db state so init_db() rebuilds against the
    temp path and drops any stale writer worker (see _reset_persist_executor).
    """
    tmp = tempfile.mkdtemp()
    os.environ.update(
        DB_PATH=os.path.join(tmp, "writer.db"),
        JBRAIN_ACCESS_KEY="k" * 40,
        BRAIN_NAME="Test",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()
    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    # A scratch table the write units touch. Created + committed up front on the test thread's
    # connection so the units do only plain INSERTs.
    c = db.get_conn()
    c.execute("CREATE TABLE IF NOT EXISTS _w(id INTEGER PRIMARY KEY AUTOINCREMENT, who TEXT)")
    c.commit()
    return db


def test_concurrent_writers_never_lock(fresh_db):
    """Many threads submitting writes at once all succeed — no 'database is locked', none lost.

    With a single writer connection there is nothing to contend with, so every one of the N writes
    commits exactly once. Pre-change, concurrent writers raced the WAL write lock and a loser could
    time out with 'database is locked'.
    """
    db = fresh_db
    N = 50

    def _one(i):
        """Submit a single-row INSERT as a write unit; return its new rowid."""
        def _w():
            conn = db.get_conn()          # the writer's own connection (thread-local on the worker)
            cur = conn.execute("INSERT INTO _w(who) VALUES (?)", (f"t{i}",))
            conn.commit()
            return cur.lastrowid
        return db.submit_write(_w)

    # Fire all submissions from many threads simultaneously to maximise contention pressure.
    futures = []
    lock = threading.Lock()

    def _fire(i):
        f = _one(i)
        with lock:
            futures.append(f)

    threads = [threading.Thread(target=_fire, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    done, not_done = wait(futures, timeout=30)
    assert not not_done, "some write units never completed — the single writer wedged"
    rowids = [f.result() for f in futures]           # re-raises any 'database is locked'
    assert len(set(rowids)) == N, "every write must get a distinct rowid"
    assert db.get_conn().execute("SELECT COUNT(*) FROM _w").fetchone()[0] == N


def test_submit_write_preserves_submission_order(fresh_db):
    """Sequential submissions from one thread apply FIFO — the single worker drains in order."""
    db = fresh_db

    def _ins(tag):
        """Write unit inserting one tagged row."""
        def _w():
            conn = db.get_conn()
            conn.execute("INSERT INTO _w(who) VALUES (?)", (tag,))
            conn.commit()
        return db.submit_write(_w)

    futures = [_ins(f"n{i}") for i in range(10)]
    wait(futures, timeout=30)
    for f in futures:
        f.result()
    order = [r["who"] for r in db.get_conn().execute("SELECT who FROM _w ORDER BY id")]
    assert order == [f"n{i}" for i in range(10)]


def test_result_and_exception_flow_through_the_future(fresh_db):
    """A write unit's return value resolves the Future; its exception re-raises on .result()."""
    db = fresh_db

    def _w_ok():
        conn = db.get_conn()
        cur = conn.execute("INSERT INTO _w(who) VALUES ('ok')")
        conn.commit()
        return cur.lastrowid

    rowid = db.submit_write(_w_ok).result(timeout=30)
    assert isinstance(rowid, int) and rowid >= 1

    def _w_boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        db.submit_write(_w_boom).result(timeout=30)


def test_unit_that_raises_mid_transaction_does_not_leak_to_the_next_unit(fresh_db):
    """A unit raising after a write rolls back, so the next unit starts on a clean connection.

    Without the rollback safety net, the failed unit's open transaction would survive on the
    shared single-writer connection and the NEXT unit would inherit it — committing the failed
    unit's partial write and/or reading a stale snapshot.
    """
    db = fresh_db

    def _w_raise_after_write():
        conn = db.get_conn()
        conn.execute("INSERT INTO _w(who) VALUES ('leaked')")   # opens a transaction, never committed
        raise RuntimeError("fail after a partial write")

    with pytest.raises(RuntimeError, match="fail after a partial write"):
        db.submit_write(_w_raise_after_write).result(timeout=30)

    # The partial write was rolled back, and the writer connection carries no open transaction
    # into the next unit.
    def _probe():
        conn = db.get_conn()
        in_txn_before = conn.in_transaction
        n = conn.execute("SELECT COUNT(*) FROM _w WHERE who='leaked'").fetchone()[0]
        return in_txn_before, n

    in_txn_before, leaked = db.submit_write(_probe).result(timeout=30)
    assert in_txn_before is False, "next unit inherited an open transaction from the failed unit"
    assert leaked == 0, "the failed unit's partial write was not rolled back"


def test_submit_write_from_a_thread_holding_a_write_lock_fails_fast(fresh_db):
    """Calling submit_write while the caller holds an open write txn raises, not deadlocks.

    The caller's connection holds the single SQLite write lock; the writer would block on it up to
    busy_timeout while the caller blocks in .result() waiting for the writer — a 60s self-deadlock.
    The guard turns that into an immediate, localized ProgrammingError.
    """
    import sqlite3
    db = fresh_db
    conn = db.get_conn()
    conn.execute("INSERT INTO _w(who) VALUES ('holding the lock')")   # open write txn, NOT committed
    try:
        with pytest.raises(sqlite3.ProgrammingError, match="open write transaction"):
            db.submit_write(lambda: None)
    finally:
        conn.rollback()
    # After rollback, the same call works (no held lock).
    assert db.submit_write(lambda: db.get_conn().execute("SELECT 1") and None).result(timeout=30) is None


def test_nested_write_runs_inline_without_deadlock(fresh_db):
    """A write unit that itself calls submit_write must NOT deadlock the single worker.

    The inner submit detects it is already on the writer thread and runs inline, so both rows are
    written within the one outer unit. Without the re-entrancy guard the inner .result() would
    block the only worker on itself forever.
    """
    db = fresh_db

    def _outer():
        conn = db.get_conn()
        conn.execute("INSERT INTO _w(who) VALUES ('outer')")

        def _inner():
            # Same writer connection (we're on the writer thread); part of the outer unit.
            db.get_conn().execute("INSERT INTO _w(who) VALUES ('inner')")
            return "inner-done"

        inner_result = db.submit_write(_inner).result(timeout=5)   # runs INLINE, no enqueue
        conn.commit()
        return inner_result

    assert db.submit_write(_outer).result(timeout=30) == "inner-done"
    rows = {r["who"] for r in db.get_conn().execute("SELECT who FROM _w")}
    assert {"outer", "inner"} <= rows


def test_writer_reset_hook_clears_stale_state_before_each_unit(fresh_db):
    """A reset hook runs before every unit, wiping writer-thread-local state a prior unit left.

    Mirrors the notes deferred-entry-event leak: a unit that stashes per-call state on the SHARED
    writer thread and then RAISES would leave a straggler for the next unit to misfire on. The
    on_writer_reset hook clears it before each unit so no straggler survives.
    """
    db = fresh_db
    leaked: dict = {}            # stands in for a service's writer-thread-local state
    hook = leaked.clear
    db._writer_reset_hooks.append(hook)
    try:
        def _stash_then_fail():
            leaked["ghost"] = 1   # recorded on the writer thread
            raise RuntimeError("boom after stashing, before draining")

        with pytest.raises(RuntimeError, match="boom after stashing"):
            db.submit_write(_stash_then_fail).result(timeout=30)

        # The next unit must NOT see the straggler — the reset hook ran before it.
        observed = db.submit_write(lambda: dict(leaked)).result(timeout=30)
        assert observed == {}, "stale writer-thread state leaked into the next unit"
    finally:
        db._writer_reset_hooks.remove(hook)   # leave the registry (incl. notes' real hook) intact
