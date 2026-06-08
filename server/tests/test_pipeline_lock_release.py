"""Regression: a SCHEDULED pipeline must not hold SQLite's single write lock across an LLM call.

This is the root cause of "JBrain stops responding to assistant messages": the nightly batches
(e.g. analyze_notes' `for_each` over `analyze_note`, and the wiki_update / wiki_maintain article
loops) wrote each unit WITHOUT committing, while run_workflow committed only once at the very
end. So one write transaction stayed open across every model call in the batch — pinning the
write lock for the batch's whole multi-minute duration. A concurrent interactive write (a chat's
user-turn INSERT) then waited out busy_timeout and failed with "database is locked", and the
reply silently aborted. The fix commits between steps/items in scheduled runs (commit_steps=True)
so the lock is released between LLM calls; synchronous in-transaction callers (commit_steps=False)
keep their single transaction.

These tests assert the invariant at the pipeline boundary, where the bug actually lived.
"""
import os
import tempfile

import pytest


@pytest.fixture()
def conn():
    tmp = tempfile.mkdtemp()
    os.environ.update(
        DB_PATH=os.path.join(tmp, "lock.db"),
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
    c = db.get_conn()
    # A scratch table the probe writes to. Created + committed up front so the probe does only
    # plain DML (INSERT) — making the transaction state it observes unambiguous.
    c.execute("CREATE TABLE IF NOT EXISTS _lock_probe(n INTEGER)")
    c.commit()
    return c


# A for_each over three items, each running one write step — the exact shape of the batches that
# caused the bug (analyze_notes / wiki_update run analyze_note per note inside a for_each).
_RECIPE = {"type": "probe", "steps": [
    {"for_each": "{{ [1, 2, 3] }}", "steps": [{"do": "_lock_probe", "with": {"n": "{{ item }}"}}]},
]}


def _install_probe(pipeline, seen):
    """Register a primitive that records whether a write transaction is ALREADY open when the
    step starts (i.e. the previous write step's lock is still held), then writes a row itself."""
    def probe(ctx, n=None):
        seen.append(ctx.conn.in_transaction)
        ctx.conn.execute("INSERT INTO _lock_probe(n) VALUES (?)", (int(n),))
        return {"n": n}
    pipeline._PRIMITIVES["_lock_probe"] = probe


def test_scheduled_run_releases_write_lock_between_items(conn):
    """commit_steps=True (a scheduled run): each item's write is committed before the next item,
    so every step starts with NO open write transaction — the lock is free between LLM calls and
    a concurrent chat write would never block."""
    from app.services import pipeline
    seen: list[bool] = []
    _install_probe(pipeline, seen)
    try:
        pipeline.run_pipeline(conn, _RECIPE, {}, None, None, commit_steps=True)
    finally:
        pipeline._PRIMITIVES.pop("_lock_probe", None)
    assert seen == [False, False, False], seen
    assert not conn.in_transaction  # fully flushed at the end


def test_synchronous_run_keeps_single_transaction(conn):
    """commit_steps=False (the default for synchronous in-transaction callers): writes accumulate
    in one transaction, so after the first write later items see it still OPEN. This documents the
    behaviour the caller opts into — and is exactly the lock-holding state the scheduled path must
    NOT exhibit (the test above)."""
    from app.services import pipeline
    seen: list[bool] = []
    _install_probe(pipeline, seen)
    try:
        pipeline.run_pipeline(conn, _RECIPE, {}, None, None, commit_steps=False)
        assert seen == [False, True, True], seen
        assert conn.in_transaction  # the whole batch is still uncommitted — caller owns the commit
    finally:
        pipeline._PRIMITIVES.pop("_lock_probe", None)
        conn.rollback()
