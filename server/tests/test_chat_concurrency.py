"""Concurrency regression: simultaneous chat turns must not race one SQLite connection.

The architect offloads its DB writes (the user turn + the reply/steps) to pool threads via
``asyncio.to_thread``. The bug: those closures used the connection captured from the event-loop
thread. Every async handler shares ONE thread-local loop connection, so two turns streaming at
once (two tabs, or a retry while the first is still going) would each offload a write onto two
different pool threads driving that single ``sqlite3.Connection`` concurrently — raising
"recursive use of cursors not allowed" / wedging the transaction, i.e. a failed or hung chat.

The fix: each offloaded closure uses ``get_conn()`` *inside* the worker thread (thread-local), so
concurrent turns never share a connection. This test drives several turns concurrently against a
real on-disk DB and asserts every one persists cleanly.
"""
import os

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")
pytest.importorskip("anthropic")

pytestmark = pytest.mark.concurrency

TEST_KEY = "test-access-key-1234567890"


@pytest.fixture()
def dbenv(tmp_path, monkeypatch):
    """A real ON-DISK DB so concurrent turns contend on actual connections, with embeddings stubbed."""
    os.environ.update(
        DB_PATH=str(tmp_path / "chatconc.db"),
        JBRAIN_ACCESS_KEY=TEST_KEY,
        BRAIN_NAME="Test Brain",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()
    from app.services import embeddings
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "embed_many",
                        lambda ts: [[0.0] * embeddings.EMBEDDING_DIM for _ in ts])
    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    yield
    db._local.__dict__.clear()


class _ChattyProvider:
    """One-turn reply, no tools — fast to reach the offloaded persist."""

    def has_credentials(self): return True
    def default_model(self): return "x"
    def supports_tools(self): return True

    async def stream_turn(self, messages, *, system, tools, model, max_tokens):
        import asyncio
        from app.services.llm import TextDelta, TurnEnd
        await asyncio.sleep(0)   # yield control so gathered turns interleave around the persist
        yield TextDelta("A concise reply that is long enough to be persisted as the turn's answer.")
        yield TurnEnd([], usage=None)

    def append_tool_results(self, messages, results): pass


def test_concurrent_turns_persist_without_connection_race(dbenv, monkeypatch):
    import asyncio
    from app.db import get_conn
    from app.services import architect, llm

    monkeypatch.setattr(llm, "get_provider", lambda *a, **k: _ChattyProvider())

    conn = get_conn()
    cids = []
    for _ in range(8):
        conn.execute("INSERT INTO conversations DEFAULT VALUES")
        cids.append(conn.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()["id"])
    conn.commit()

    async def one(cid):
        async for _ in architect.run(cid, "hello there", None, "assisted"):
            pass

    async def go():
        # gather() interleaves the turns on one event loop; each offloads its persist to the
        # shared thread pool, so the writes land on worker threads at overlapping times.
        await asyncio.gather(*(one(cid) for cid in cids))

    asyncio.run(go())   # pre-fix: raises a sqlite ProgrammingError/OperationalError from the race

    # Every turn persisted exactly one assistant reply.
    rows = get_conn().execute(
        "SELECT conversation_id, COUNT(*) c FROM messages WHERE role='assistant' GROUP BY conversation_id"
    ).fetchall()
    by_conv = {r["conversation_id"]: r["c"] for r in rows}
    assert all(by_conv.get(cid) == 1 for cid in cids), by_conv
