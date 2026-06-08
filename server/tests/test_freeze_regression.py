"""Group A regression: the image/audio analysis workers must never hold the SQLite write
lock across the (multi-second) embedding compute, and a failed embed must still commit a
terminal summary (self-healed later by the startup backfill).

The original bug: the worker ran `embed_many` INSIDE its `BEGIN IMMEDIATE` transaction, so a
slow/cold embed held the single WAL write lock; every other writer — including the owner chat
persisting its turn on the event loop — then blocked on busy_timeout (5s) and failed with
"database is locked", i.e. "all the AI went unresponsive". These tests pin the fix: the embed
runs OUTSIDE the lock, so a concurrent writer proceeds immediately while an embed blocks.
"""
import os
import sqlite3
import threading
import time

import pytest

pytest.importorskip("sqlite_vec")

pytestmark = pytest.mark.concurrency

TEST_KEY = "test-access-key-1234567890"


@pytest.fixture()
def dbenv(tmp_path):
    """A real ON-DISK DB (so two threads/connections can contend for the write lock)."""
    os.environ.update(
        DB_PATH=str(tmp_path / "freeze.db"),
        JBRAIN_ACCESS_KEY=TEST_KEY,
        BRAIN_NAME="Test Brain",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()
    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    yield
    db._local.__dict__.clear()


def _seed_note_and_image(conn, *, mime="image/png", status="pending"):
    conn.execute("INSERT INTO notes (slug, title, content_md) VALUES ('n', 'notes/n', 'Body.')")
    nid = conn.execute("SELECT id FROM notes WHERE slug='n'").fetchone()["id"]
    cur = conn.execute(
        "INSERT INTO attachments (note_id, filename, mime, content_blob, byte_size, sha256, analysis_status) "
        "VALUES (?,?,?,?,?,?,?)",
        (nid, "pic.png", mime, b"x", 1, "sha-pic", status),
    )
    conn.commit()
    return nid, cur.lastrowid


def _fts_has(conn, att_id):
    return conn.execute(
        "SELECT COUNT(*) c FROM attachments_fts WHERE attachment_id=?", (att_id,)
    ).fetchone()["c"]


def _chunk_count(conn, att_id):
    return conn.execute(
        "SELECT COUNT(*) c FROM attachment_chunks WHERE attachment_id=?", (att_id,)
    ).fetchone()["c"]


def test_image_worker_holds_no_lock_during_embed(dbenv, monkeypatch):
    """While the embed blocks, a concurrent writer must acquire BEGIN IMMEDIATE immediately —
    proving the worker holds NO write lock across the embed (the freeze fix)."""
    from app.db import get_conn
    from app.services import image_analysis as ia, embeddings

    conn = get_conn()
    nid, aid = _seed_note_and_image(conn)

    monkeypatch.setattr(ia, "_vision_summary", lambda *a, **k: "A blue surfboard on sand.")
    started, release = threading.Event(), threading.Event()

    def blocking_embed(chunks):
        started.set()
        assert release.wait(5), "test did not release the embed"
        return [[0.0] * embeddings.EMBEDDING_DIM for _ in chunks]

    monkeypatch.setattr(embeddings, "embed_attachment_chunks", blocking_embed)

    worker = threading.Thread(target=ia.analyze, args=(aid,), daemon=True)
    worker.start()
    assert started.wait(5), "embed never started"

    # The embed is now blocking on its own thread. A concurrent writer (the chat's offloaded
    # message INSERT, simulated here) must NOT be blocked — pre-fix it would wait out the full
    # 5s busy_timeout and raise OperationalError("database is locked").
    t0 = time.time()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE notes SET title = title WHERE id = ?", (nid,))
    conn.commit()
    assert time.time() - t0 < 1.0, "a concurrent write was blocked → the worker held the lock across the embed"

    release.set()
    worker.join(5)
    row = conn.execute(
        "SELECT analysis_status, analysis_md FROM attachments WHERE id=?", (aid,)
    ).fetchone()
    assert row["analysis_status"] == "done"
    assert "A blue surfboard on sand." in row["analysis_md"]
    # Summary + FTS + vectors all landed (and the embed result was written).
    assert _fts_has(conn, aid) == 1
    assert _chunk_count(conn, aid) >= 1


def test_image_summary_survives_embed_failure_and_self_heals(dbenv, monkeypatch):
    """An embed that RAISES must NOT lose the summary: status reaches 'done', the FTS row is
    written (keyword search works), and the missing vectors are backfilled later by the
    startup reindex (semantic search recovers on the next boot)."""
    from app.db import get_conn
    from app.services import image_analysis as ia, embeddings

    conn = get_conn()
    nid, aid = _seed_note_and_image(conn)

    monkeypatch.setattr(ia, "_vision_summary", lambda *a, **k: "Summary text here.")

    def boom(chunks):
        raise RuntimeError("embed model wedged")

    monkeypatch.setattr(embeddings, "embed_attachment_chunks", boom)
    ia.analyze(aid)  # inline on this thread (owns_conn False) → uses `conn`

    row = conn.execute(
        "SELECT analysis_status, analysis_md FROM attachments WHERE id=?", (aid,)
    ).fetchone()
    assert row["analysis_status"] == "done"          # terminal status reached despite the failure
    assert "Summary text here." in row["analysis_md"]  # summary preserved
    assert _fts_has(conn, aid) == 1                   # keyword search works immediately
    assert _chunk_count(conn, aid) == 0               # vectors deferred (not lost)
    assert conn.in_transaction is False               # lock released, no dangling txn

    # Self-heal: the boot-time backfill fills the missing vectors (with a now-working embed).
    monkeypatch.setattr(embeddings, "embed_attachment_chunks",
                        lambda chunks: [[0.0] * embeddings.EMBEDDING_DIM for _ in chunks])
    healed = embeddings.reindex_missing_attachment_analysis(conn)
    assert healed == 1
    assert _chunk_count(conn, aid) >= 1               # semantic half now present


def test_image_normal_path_commits_summary_fts_and_vectors_together(dbenv, monkeypatch):
    """The healthy path is fully consistent in one commit — no reliance on the backfill."""
    from app.db import get_conn
    from app.services import image_analysis as ia, embeddings

    conn = get_conn()
    nid, aid = _seed_note_and_image(conn)
    monkeypatch.setattr(ia, "_vision_summary", lambda *a, **k: "Normal summary.")
    monkeypatch.setattr(embeddings, "embed_attachment_chunks",
                        lambda chunks: [[0.0] * embeddings.EMBEDDING_DIM for _ in chunks])
    ia.analyze(aid)

    row = conn.execute(
        "SELECT analysis_status, analysis_md FROM attachments WHERE id=?", (aid,)
    ).fetchone()
    assert row["analysis_status"] == "done"
    assert "Normal summary." in row["analysis_md"]
    assert _fts_has(conn, aid) == 1
    assert _chunk_count(conn, aid) >= 1               # vectors present immediately


def test_audio_worker_holds_no_lock_during_embed(dbenv, monkeypatch):
    """The audio/video transcription sibling had the identical latent bug — prove it too."""
    from app.db import get_conn
    from app.services import audio_transcription as at, embeddings

    conn = get_conn()
    conn.execute("INSERT INTO notes (slug, title, content_md) VALUES ('a', 'notes/a', 'Body.')")
    nid = conn.execute("SELECT id FROM notes WHERE slug='a'").fetchone()["id"]
    aid = conn.execute(
        "INSERT INTO attachments (note_id, filename, mime, content_blob, byte_size, sha256, analysis_status) "
        "VALUES (?,?,?,?,?,?,'pending')",
        (nid, "clip.mp3", "audio/mpeg", b"x", 1, "sha-clip"),
    ).lastrowid
    conn.commit()

    monkeypatch.setattr(at, "_transcribe", lambda raw: "hello world transcript")
    started, release = threading.Event(), threading.Event()

    def blocking_embed(chunks):
        started.set()
        assert release.wait(5)
        return [[0.0] * embeddings.EMBEDDING_DIM for _ in chunks]

    monkeypatch.setattr(embeddings, "embed_attachment_chunks", blocking_embed)

    worker = threading.Thread(target=at.transcribe, args=(aid,), daemon=True)
    worker.start()
    assert started.wait(5), "audio embed never started"

    t0 = time.time()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE notes SET title = title WHERE id = ?", (nid,))
    conn.commit()
    assert time.time() - t0 < 1.0, "audio worker held the lock across the embed"

    release.set()
    worker.join(5)
    row = conn.execute("SELECT analysis_status, analysis_md FROM attachments WHERE id=?", (aid,)).fetchone()
    assert row["analysis_status"] == "done"
    assert "hello world transcript" in row["analysis_md"]
    assert _chunk_count(conn, aid) >= 1
