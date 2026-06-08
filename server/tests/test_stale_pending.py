"""Group C regression: a stuck analysis_status='pending' must be recoverable, and a worker
finishing after a watchdog reap must NOT resurrect the reaped row.

The worker sets 'done' only at its final commit; if it hangs without raising (a wedged model
load), the row stays 'pending' forever and the UI shows a permanent spinner. A periodic
time-thresholded watchdog (reset_stale with older_than_seconds) reaps it to 'error' so it
becomes retryable; the worker's guarded final commit (abandon if the row is already terminal)
guarantees exactly one terminal status survives.
"""
import os

import pytest

pytest.importorskip("sqlite_vec")

pytestmark = pytest.mark.concurrency

TEST_KEY = "test-access-key-1234567890"


@pytest.fixture()
def dbenv(tmp_path):
    os.environ.update(
        DB_PATH=str(tmp_path / "stale.db"),
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


def _add(conn, *, mime="image/png", status="pending", analyzed_at=None, ago_seconds=None,
         analysis_md=None):
    """Insert an attachment; analyzed_at can be an explicit value or 'now minus ago_seconds'."""
    if ago_seconds is not None:
        analyzed_at_sql = f"strftime('%Y-%m-%d %H:%M:%f','now','-{int(ago_seconds)} seconds')"
    elif analyzed_at is not None:
        analyzed_at_sql = "?"
    else:
        analyzed_at_sql = "NULL"
    sql = ("INSERT INTO attachments (filename, mime, byte_size, sha256, analysis_status, analysis_md, analyzed_at) "
           f"VALUES (?,?,?,?,?,?, {analyzed_at_sql})")
    params = ["f.bin", mime, 1, f"sha-{status}-{mime}-{ago_seconds}-{analyzed_at}", status, analysis_md]
    if analyzed_at_sql == "?":
        params.append(analyzed_at)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid


def _status(conn, aid):
    return conn.execute("SELECT analysis_status, analysis_detail, analysis_md, analyzed_at "
                        "FROM attachments WHERE id=?", (aid,)).fetchone()


def test_watchdog_reaps_over_threshold_pending(dbenv):
    from app.db import get_conn
    from app.services import image_analysis as ia
    conn = get_conn()
    aid = _add(conn, status="pending", ago_seconds=2000)   # > 1800s threshold
    n = ia.reset_stale(conn, older_than_seconds=ia.STALE_PENDING_SECONDS)
    assert n == 1
    row = _status(conn, aid)
    assert row["analysis_status"] == "error"
    assert "timed out" in (row["analysis_detail"] or "")


def test_watchdog_reaps_audio_too_shared_columns(dbenv):
    from app.db import get_conn
    from app.services import image_analysis as ia
    conn = get_conn()
    aid = _add(conn, mime="audio/mpeg", status="pending", ago_seconds=2000)
    assert ia.reset_stale(conn, older_than_seconds=1800) == 1   # one sweep covers transcription too
    assert _status(conn, aid)["analysis_status"] == "error"


def test_watchdog_preserves_recent_pending(dbenv):
    from app.db import get_conn
    from app.services import image_analysis as ia
    conn = get_conn()
    aid = _add(conn, status="pending", ago_seconds=30)        # well within threshold → in-flight
    assert ia.reset_stale(conn, older_than_seconds=1800) == 0
    assert _status(conn, aid)["analysis_status"] == "pending"


def test_watchdog_ignores_terminal_rows(dbenv):
    from app.db import get_conn
    from app.services import image_analysis as ia
    conn = get_conn()
    done = _add(conn, status="done", ago_seconds=9000)
    err = _add(conn, status="error", ago_seconds=9000)
    assert ia.reset_stale(conn, older_than_seconds=1800) == 0
    assert _status(conn, done)["analysis_status"] == "done"
    assert _status(conn, err)["analysis_status"] == "error"


def test_boot_reset_stale_blankets_all_pending(dbenv):
    from app.db import get_conn
    from app.services import image_analysis as ia
    conn = get_conn()
    recent = _add(conn, status="pending", ago_seconds=5)     # even a fresh pending is from a dead process at boot
    old = _add(conn, status="pending", ago_seconds=9000)
    assert ia.reset_stale(conn) == 2                          # no threshold → reset ALL
    assert _status(conn, recent)["analysis_status"] == "error"
    assert _status(conn, old)["analysis_status"] == "error"


def test_set_status_stamps_analyzed_at_on_pending(dbenv):
    from app.db import get_conn
    from app.services import image_analysis as ia, audio_transcription as at
    conn = get_conn()
    for svc in (ia, at):
        aid = _add(conn, status="none")
        conn.execute("UPDATE attachments SET analyzed_at = NULL WHERE id = ?", (aid,))
        conn.commit()
        svc._set_status(conn, aid, "pending")
        conn.commit()
        assert _status(conn, aid)["analyzed_at"] is not None   # pending now carries a start clock
        # And done overwrites it (completion time).
        before = _status(conn, aid)["analyzed_at"]
        svc._set_status(conn, aid, "done")
        conn.commit()
        assert _status(conn, aid)["analyzed_at"] is not None and _status(conn, aid)["analyzed_at"] >= before


def test_worker_abandons_reaped_row_no_resurrection(dbenv, monkeypatch):
    """A worker that finishes AFTER the watchdog reaped its row to 'error' must NOT flip it back
    to 'done' (the guarded final commit). The reaped row stays 'error' (recoverable)."""
    from app.db import get_conn
    from app.services import image_analysis as ia
    conn = get_conn()
    aid = _add(conn, status="error", ago_seconds=2000)        # already reaped
    monkeypatch.setattr(ia, "_vision_summary", lambda *a, **k: "late summary from a zombie worker")
    # Give it real bytes so the worker reaches the terminal commit, where the guard fires.
    conn.execute("UPDATE attachments SET content_blob = ?, note_id = NULL WHERE id = ?", (b"x", aid))
    conn.commit()

    ia.analyze(aid)   # computes a summary, then re-reads 'error' inside BEGIN IMMEDIATE → abandons

    row = _status(conn, aid)
    assert row["analysis_status"] == "error"      # NOT resurrected to 'done'
    assert row["analysis_md"] is None             # the stale summary was NOT written


def test_worker_writes_when_it_owns_a_pending_row(dbenv, monkeypatch):
    """The complement: a worker on a 'pending' row it owns DOES write (guard passes)."""
    from app.db import get_conn
    from app.services import image_analysis as ia, embeddings
    conn = get_conn()
    aid = _add(conn, status="pending", ago_seconds=5)
    conn.execute("UPDATE attachments SET content_blob = ?, note_id = NULL WHERE id = ?", (b"x", aid))
    conn.commit()
    monkeypatch.setattr(ia, "_vision_summary", lambda *a, **k: "a real summary")
    monkeypatch.setattr(embeddings, "embed_attachment_chunks",
                        lambda chunks: [[0.0] * embeddings.EMBEDDING_DIM for _ in chunks])
    ia.analyze(aid)
    row = _status(conn, aid)
    assert row["analysis_status"] == "done"
    assert "a real summary" in row["analysis_md"]


def test_force_restarts_a_stale_pending(dbenv, monkeypatch):
    """start_analysis must let force=True restart a wedged 'pending' (otherwise it short-circuits
    on 'pending' and the row can never be retried)."""
    from app.db import get_conn
    from app.services import image_analysis as ia, llm
    conn = get_conn()
    aid = _add(conn, status="pending", ago_seconds=2000)
    conn.execute("UPDATE attachments SET content_blob = ? WHERE id = ?", (b"x", aid))
    conn.commit()
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    # Don't actually run a worker — just prove force gets past the pending short-circuit.
    spawned = {"n": 0}

    class _NoThread:
        def __init__(self, *a, **k): pass
        def start(self): spawned["n"] += 1

    monkeypatch.setattr(ia.threading, "Thread", _NoThread)

    # Without force: short-circuits, no restart.
    assert ia.start_analysis(conn, aid, force=False) == {"status": "pending"}
    assert spawned["n"] == 0
    # With force: re-marks pending (fresh analyzed_at) and spawns a fresh worker.
    assert ia.start_analysis(conn, aid, force=True) == {"status": "pending"}
    assert spawned["n"] == 1
