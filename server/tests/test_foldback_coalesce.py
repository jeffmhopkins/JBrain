"""Group B regression: the per-image / per-audio note-analysis fold-back must COALESCE.

N attachments landing on one note used to each spend a fresh whole-note cheap-LLM
re-analysis (each new summary changes the attachment-context hash, so the hash guard
can't collapse them). note_analysis.request_fold collapses a burst to ~one analysis
that still reflects EVERY summary, never dropping the last — and never wedges when an
image errors (the case the naive "am I the last image?" check failed).
"""
import os
import threading
import time

import pytest

pytest.importorskip("sqlite_vec")

pytestmark = pytest.mark.concurrency

TEST_KEY = "test-access-key-1234567890"


@pytest.fixture()
def dbenv(tmp_path):
    os.environ.update(
        DB_PATH=str(tmp_path / "fold.db"),
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
    # Reset the process-local coalescer state between tests.
    from app.services import note_analysis as na
    na._fold_inflight.clear()
    na._fold_pending.clear()
    na.fold_run_inline = False
    yield
    na._fold_inflight.clear()
    na._fold_pending.clear()
    na.fold_run_inline = False
    db._local.__dict__.clear()


def _enable_auto_analyze(conn):
    # The repo workflow isn't seeded in this minimal DB env, so upsert it enabled.
    conn.execute(
        "INSERT INTO workflows (key, name, trigger_type, action_type, enabled) "
        "VALUES ('analyze-new-note', 'Analyze new note', 'event', 'analyze', 1) "
        "ON CONFLICT(key) DO UPDATE SET enabled = 1")
    conn.commit()


def _seed_note(conn, slug="trip"):
    conn.execute("INSERT INTO notes (slug, title, content_md, kind) VALUES (?,?,?, 'entry')",
                 (slug, f"notes/{slug}", "A trip."))
    return conn.execute("SELECT id FROM notes WHERE slug=?", (slug,)).fetchone()["id"]


def _add_image(conn, nid, name, status="pending"):
    return conn.execute(
        "INSERT INTO attachments (note_id, filename, mime, content_blob, byte_size, sha256, analysis_status) "
        "VALUES (?,?,?,?,?,?,?)",
        (nid, name, "image/png", b"x", 1, "sha-" + name, status),
    ).lastrowid


def _patch_llm(monkeypatch, note_calls):
    """Stub llm so a per-image vision call returns a per-file summary, and count the
    note-analysis (cheap, text-prompt) calls separately."""
    from app.services import image_analysis as ia, llm

    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    monkeypatch.setattr(llm, "model_for", lambda *a: "m")
    # Vision summary: deterministic per-filename marker, no Pillow.
    monkeypatch.setattr(ia, "_vision_summary", lambda raw, fn, *a, **k: f"summary-of-{fn}")

    def fake_complete(messages, **k):
        content = messages[0]["content"]
        if isinstance(content, str):          # note_analysis prompt is a plain string
            note_calls.append(content)
            return '{"gist":"g","facts":["f"],"entities":[],"domain":"Things","dates":[]}'
        return "unused"                        # vision goes through _vision_summary above
    monkeypatch.setattr(llm, "complete", fake_complete)


def test_three_images_coalesce_to_one_note_analysis_inline(dbenv, monkeypatch):
    """Sequential inline workers + inline fold: the note analysis ends reflecting all 3
    summaries, and the fold is hash-guarded so it isn't re-spent once the context settles."""
    from app.db import get_conn
    from app.services import image_analysis as ia, note_analysis as na
    conn = get_conn()
    _enable_auto_analyze(conn)
    monkeypatch.setattr(na, "fold_run_inline", True)
    note_calls = []
    _patch_llm(monkeypatch, note_calls)

    nid = _seed_note(conn)
    ids = [_add_image(conn, nid, f"img{i}.png") for i in (1, 2, 3)]
    conn.commit()

    for aid in ids:
        ia.analyze(aid)   # inline (shared conn); each commits its summary then folds inline

    # All three summaries are stored on the attachments.
    md = [r["analysis_md"] for r in conn.execute(
        "SELECT analysis_md FROM attachments WHERE note_id=? ORDER BY id", (nid,)).fetchall()]
    assert md == ["summary-of-img1.png", "summary-of-img2.png", "summary-of-img3.png"]

    # The LAST fold's prompt included ALL three summaries (never dropped the final image).
    assert note_calls, "no note-analysis fold ran"
    last = note_calls[-1]
    assert "summary-of-img1.png" in last
    assert "summary-of-img2.png" in last
    assert "summary-of-img3.png" in last
    # A stored analysis exists, and the count is bounded (≤3, the sequential-inline upper bound),
    # never a runaway. The point is the final state is complete.
    assert na.get(conn, nid) is not None
    assert len(note_calls) <= 3


def test_three_images_real_threads_coalesce(dbenv, monkeypatch):
    """The real concurrency path (fold_run_inline False): 3 workers finishing near-together
    collapse to a SMALL number of note-analysis calls (never 3+), and the final stored analysis
    reflects all three summaries."""
    from app.db import get_conn
    from app.services import image_analysis as ia, note_analysis as na
    conn = get_conn()
    _enable_auto_analyze(conn)
    note_calls = []
    lock = threading.Lock()

    from app.services import llm
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    monkeypatch.setattr(llm, "model_for", lambda *a: "m")
    monkeypatch.setattr(ia, "_vision_summary", lambda raw, fn, *a, **k: f"summary-of-{fn}")

    def fake_complete(messages, **k):
        content = messages[0]["content"]
        if isinstance(content, str):
            time.sleep(0.05)                  # widen the window so a sibling lands mid-analysis
            with lock:
                note_calls.append(content)
            return '{"gist":"g","facts":["f"],"entities":[],"domain":"Things","dates":[]}'
        return "unused"
    monkeypatch.setattr(llm, "complete", fake_complete)

    nid = _seed_note(conn)
    ids = [_add_image(conn, nid, f"img{i}.png") for i in (1, 2, 3)]
    conn.commit()

    barrier = threading.Barrier(3)

    def run(aid):
        barrier.wait()                        # release all three together
        ia.analyze(aid)                       # own thread → own conn (owns_conn True)

    threads = [threading.Thread(target=run, args=(aid,)) for aid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    # Wait for the coalescer to drain.
    deadline = time.time() + 10
    while time.time() < deadline:
        with na._fold_lock:
            if not na._fold_inflight and not na._fold_pending:
                break
        time.sleep(0.02)

    with lock:
        n = len(note_calls)
    assert 1 <= n <= 2, f"expected coalesced (1-2) note-analysis calls, got {n}"
    # The final stored analysis reflects all three summaries (final image never dropped).
    last = note_calls[-1]
    assert all(f"summary-of-img{i}.png" in last for i in (1, 2, 3)), last
    assert na.get(conn, nid) is not None


def test_errored_last_image_does_not_wedge_foldback(dbenv, monkeypatch):
    """If the LAST image to run ERRORS, the earlier successful images' fold-back must STILL run
    and include their summaries — the case the naive 'last finisher folds' design dropped."""
    from app.db import get_conn
    from app.services import image_analysis as ia, note_analysis as na
    conn = get_conn()
    _enable_auto_analyze(conn)
    monkeypatch.setattr(na, "fold_run_inline", True)
    note_calls = []

    from app.services import llm
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    monkeypatch.setattr(llm, "model_for", lambda *a: "m")

    def vision(raw, fn, *a, **k):
        if fn == "bad.png":
            raise ia.UnsupportedImage("cannot decode")
        return f"summary-of-{fn}"
    monkeypatch.setattr(ia, "_vision_summary", vision)

    def fake_complete(messages, **k):
        content = messages[0]["content"]
        if isinstance(content, str):
            note_calls.append(content)
            return '{"gist":"g","facts":[],"entities":[],"domain":"Things","dates":[]}'
        return "unused"
    monkeypatch.setattr(llm, "complete", fake_complete)

    nid = _seed_note(conn)
    a1 = _add_image(conn, nid, "img1.png")
    a2 = _add_image(conn, nid, "img2.png")
    bad = _add_image(conn, nid, "bad.png")
    conn.commit()

    ia.analyze(a1)
    ia.analyze(a2)
    ia.analyze(bad)   # errors → no summary, no fold; but a1/a2 already folded

    assert conn.execute("SELECT analysis_status FROM attachments WHERE id=?", (bad,)).fetchone()[0] == "error"
    assert note_calls, "fold-back was wedged by the errored image (regression)"
    last = note_calls[-1]
    assert "summary-of-img1.png" in last and "summary-of-img2.png" in last
    assert na.get(conn, nid) is not None


def test_no_auto_analyze_skips_foldback(dbenv, monkeypatch):
    """With the master switch OFF, the image summary is still stored but NO note-analysis runs."""
    from app.db import get_conn
    from app.services import image_analysis as ia, note_analysis as na
    conn = get_conn()
    # auto-analyze NOT enabled
    monkeypatch.setattr(na, "fold_run_inline", True)
    note_calls = []
    _patch_llm(monkeypatch, note_calls)

    nid = _seed_note(conn)
    aid = _add_image(conn, nid, "solo.png")
    conn.commit()
    ia.analyze(aid)

    assert conn.execute("SELECT analysis_md FROM attachments WHERE id=?", (aid,)).fetchone()[0] == "summary-of-solo.png"
    assert note_calls == []                  # gated off → no fold
    assert na.get(conn, nid) is None


def test_pending_ids_backstop_surfaces_lost_foldback(dbenv, monkeypatch):
    """The durable backstop: a note whose attachment was enriched AFTER its last analysis (a
    fold-back lost to a crash) is re-surfaced by pending_ids so the batch reconciles it."""
    from app.db import get_conn
    from app.services import note_analysis as na
    conn = get_conn()
    nid = _seed_note(conn)
    # Stamp a note_analysis row as analyzed at an OLD time.
    conn.execute("INSERT INTO note_analysis (note_id, content_hash, analyzed_at) VALUES (?,?, '2020-01-01 00:00:00.000')",
                 (nid, "h"))
    # An attachment enriched LATER (its analyzed_at newer than the analysis) — the lost fold.
    conn.execute("INSERT INTO attachments (note_id, filename, mime, byte_size, sha256, analysis_md, analysis_status, analyzed_at) "
                 "VALUES (?,?,?,?,?,?, 'done', '2026-06-01 00:00:00.000')",
                 (nid, "late.png", "image/png", 1, "sha-late", "a fresh summary"))
    # Also bump notes.updated_at to BEFORE the analysis so the ordinary clause can't catch it.
    conn.execute("UPDATE notes SET updated_at = '2019-01-01 00:00:00.000' WHERE id=?", (nid,))
    conn.commit()

    assert nid in na.pending_ids(conn, 50), "backstop did not surface the note with a lost fold-back"
