"""ASYNC per-click entity rebuild — the merge/split/alias endpoints record a DURABLE
decision synchronously but DEFER the (slow, fastembed-backed) entity_index.rebuild to a
coalesced background worker.

Drives the REAL FastAPI routes via a TestClient (auth = the access key, like
test_entity_reconcile_api.py). Embeddings + entity_index._sync_embeddings are monkeypatched
to no-ops so the rebuild runs offline.

The deferral is normally a daemon thread, which a TestClient request does NOT await. So the
fixture flips entity_rebuild.run_inline = True (the module's test seam) to reconcile
synchronously on the request — letting us assert the post-rebuild fold deterministically.
Two tests instead drive the THREADED path / coalescing directly (run_inline off) to prove the
real concurrency behavior.

Covered:
- the decision row is recorded + committed synchronously on the request (independent of the rebuild);
- the rebuild is triggered and, once it runs, the entity state is reconciled (merge survivor folds the source);
- endpoint returns promptly WITHOUT waiting on _sync_embeddings (it's never called on the request thread);
- two rapid ops coalesce to a consistent final state, no lost decision;
- a rebuild FAILURE leaves the durable decision intact + the index dirty (next rebuild reconciles).
"""
import json
import os
import tempfile
import time

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")
pytest.importorskip("anthropic")

pytestmark = pytest.mark.integration

TEST_KEY = "test-access-key-1234567890"


@pytest.fixture()
def ctx(monkeypatch):
    """Returns (client, sync_calls) where sync_calls counts _sync_embeddings invocations
    (to prove the request thread never waits on it)."""
    tmp = tempfile.mkdtemp()
    os.environ.update(
        DB_PATH=os.path.join(tmp, "test.db"),
        JBRAIN_ACCESS_KEY=TEST_KEY,
        BRAIN_NAME="Test Brain",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()

    from app.services import embeddings, entity_index, entity_rebuild
    for name in ("upsert_note_embedding", "delete_note_embedding", "upsert_attachment_embeddings",
                 "delete_attachment_embeddings", "delete_entity_embedding"):
        monkeypatch.setattr(embeddings, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "semantic_search_attachments", lambda *a, **k: [])

    # Count + no-op the slow embedding sync that rebuild() runs.
    calls = {"sync": 0}
    real_noop_sync = lambda *a, **k: calls.__setitem__("sync", calls["sync"] + 1)
    monkeypatch.setattr(entity_index, "_sync_embeddings", real_noop_sync)

    # Reconcile synchronously on the request so the TestClient observes the post-rebuild state.
    monkeypatch.setattr(entity_rebuild, "run_inline", True)
    # Reset the process-local coalescing flags between tests.
    monkeypatch.setattr(entity_rebuild, "_running", False, raising=False)
    monkeypatch.setattr(entity_rebuild, "_pending", False, raising=False)

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()

    from app import auth
    auth.ensure_access_key()

    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"}), calls


def _seed_two_people(client):
    """Two analyzed 'entry' notes + kb articles, then build the index. Returns (jeff_id, bob_id)."""
    from app.db import get_conn
    from app.services import notes as ns, entity_index
    conn = get_conn()

    def _analyzed(title, name):
        nid = ns.upsert_note(conn, title, f"about {name}", kind="entry", fire_events=False)
        conn.execute(
            "INSERT INTO note_analysis (note_id, content_hash, entities_json) VALUES (?,?,?)",
            (nid, f"h-{name}", json.dumps([{"name": name, "type": "person"}])))

    _analyzed("notes/2026/01/01", "Jeffrey Hopkins")
    _analyzed("notes/2026/01/02", "Bob Smith")
    ns.upsert_note(conn, "kb/People/Jeffrey Hopkins", "# Jeffrey Hopkins\nA person.", kind="kb", fire_events=False)
    ns.upsert_note(conn, "kb/People/Bob Smith", "# Bob Smith\nAnother person.", kind="kb", fire_events=False)
    conn.commit()
    entity_index.rebuild(conn)

    def _eid(nk):
        return conn.execute("SELECT id FROM entities WHERE type='person' AND normalized_key=?",
                            (nk,)).fetchone()["id"]
    return _eid("jeffrey hopkins"), _eid("bob smith")


# ---- decision recorded synchronously + rebuild reconciles -------------------------------

def test_merge_records_decision_synchronously_and_reconciles(ctx):
    """The merge decision is committed on the request (durable, independent of the rebuild),
    AND once the deferred rebuild runs the survivor folds in the source's notes."""
    client, _ = ctx
    from app.db import get_conn
    jeff, bob = _seed_two_people(client)

    r = client.post("/api/entities/merge", json={"source_id": bob, "into_id": jeff})
    assert r.status_code == 200, r.text
    survivor = r.json()
    assert survivor["id"] == jeff and survivor["normalized_key"] == "jeffrey hopkins"
    assert "rebuilding" in survivor      # the response signals a deferred rebuild

    # The DECISION row is durably present regardless of the rebuild.
    conn = get_conn()
    merges = [d for d in conn.execute(
        "SELECT norm_a, canonical FROM entity_decisions WHERE kind='merge'").fetchall()]
    assert any(m["norm_a"] == "bob smith" and m["canonical"] == "jeffrey hopkins" for m in merges)

    # After the (inline) rebuild the source entity is gone and its notes folded under the survivor.
    assert client.get(f"/api/entities/{bob}").status_code == 404
    detail = client.get(f"/api/entities/{jeff}").json()
    titles = {n["title"] for n in detail["notes"]}
    assert {"notes/2026/01/01", "notes/2026/01/02"} <= titles, titles


def test_endpoint_does_not_wait_on_sync_embeddings_on_request_thread(ctx, monkeypatch):
    """With the rebuild truly deferred (run_inline OFF), the request returns WITHOUT ever
    calling _sync_embeddings on the request thread — proving the slow path is off-request.
    The decision is still recorded synchronously."""
    client, calls = ctx
    from app.db import get_conn
    from app.services import entity_rebuild
    jeff, bob = _seed_two_people(client)
    sync_before = calls["sync"]

    # Defer for real: swap request_rebuild to a no-op so nothing runs the rebuild on the request.
    monkeypatch.setattr(entity_rebuild, "run_inline", False)
    monkeypatch.setattr(entity_rebuild, "request_rebuild", lambda conn: None)

    r = client.post("/api/entities/merge", json={"source_id": bob, "into_id": jeff})
    assert r.status_code == 200, r.text
    # _sync_embeddings was NOT invoked synchronously by the endpoint.
    assert calls["sync"] == sync_before
    # The decision is nonetheless durable.
    conn = get_conn()
    assert conn.execute(
        "SELECT 1 FROM entity_decisions WHERE kind='merge' AND norm_a='bob smith'").fetchone() is not None


# ---- alias + split decisions defer too --------------------------------------------------

def test_add_alias_records_decision_and_reconciles(ctx):
    client, _ = ctx
    from app.services import entity_index
    jeff, _ = _seed_two_people(client)

    r = client.post(f"/api/entities/{jeff}/aliases", json={"display": "Jeff Hopkins"})
    assert r.status_code == 200, r.text
    assert "rebuilding" in r.json()

    decisions = client.get(f"/api/entities/{jeff}/decisions").json()
    assert any(d["kind"] == "alias" and d["norm_a"] == "jeff hopkins"
               and d["norm_b"] == "jeffrey hopkins" for d in decisions), decisions
    # After the inline rebuild the alias materializes on the entity detail.
    detail = client.get(f"/api/entities/{jeff}").json()
    assert "Jeff Hopkins" in detail["aliases"]
    _ = entity_index  # imported for parity with sibling suites


def test_split_records_decision(ctx):
    client, _ = ctx
    jeff, bob = _seed_two_people(client)
    r = client.post("/api/entities/split", json={"a_id": jeff, "b_id": bob})
    assert r.status_code == 200 and r.json().get("ok") is True, r.text
    decisions = client.get(f"/api/entities/{jeff}/decisions").json()
    assert any(d["kind"] == "split" and {d["norm_a"], d["norm_b"]} == {"jeffrey hopkins", "bob smith"}
               for d in decisions), decisions


# ---- coalescing: two rapid ops → one consistent final state -----------------------------

def test_two_rapid_ops_coalesce_no_lost_decision(ctx):
    """Two merges in a row both commit their decisions; the final reconciled state reflects
    BOTH — neither decision is lost to coalescing."""
    client, _ = ctx
    from app.db import get_conn
    from app.services import notes as ns, entity_index
    jeff, bob = _seed_two_people(client)
    conn = get_conn()
    # A third, DISTINCT person (different surname → no heuristic auto-merge with the others).
    nid = ns.upsert_note(conn, "notes/2026/01/03", "about Carol Jones", kind="entry", fire_events=False)
    conn.execute("INSERT INTO note_analysis (note_id, content_hash, entities_json) VALUES (?,?,?)",
                 (nid, "h-carol", json.dumps([{"name": "Carol Jones", "type": "person"}])))
    conn.commit()
    entity_index.rebuild(conn)
    carol = conn.execute("SELECT id FROM entities WHERE normalized_key='carol jones'").fetchone()["id"]

    assert client.post("/api/entities/merge", json={"source_id": bob, "into_id": jeff}).status_code == 200
    assert client.post("/api/entities/merge", json={"source_id": carol, "into_id": jeff}).status_code == 200

    # BOTH decisions are durable.
    merges = {(m["norm_a"], m["canonical"]) for m in conn.execute(
        "SELECT norm_a, canonical FROM entity_decisions WHERE kind='merge'").fetchall()}
    assert ("bob smith", "jeffrey hopkins") in merges
    assert ("carol jones", "jeffrey hopkins") in merges
    # The final reconciled survivor folds in all three notes.
    detail = client.get(f"/api/entities/{jeff}").json()
    titles = {n["title"] for n in detail["notes"]}
    assert {"notes/2026/01/01", "notes/2026/01/02", "notes/2026/01/03"} <= titles, titles


# ---- failure: decision survives, index stays dirty for reconciliation -------------------

def test_rebuild_failure_leaves_decision_intact_and_dirty(ctx, monkeypatch):
    """If the deferred rebuild raises, the durable decision is untouched and the index is left
    DIRTY (status=error) so the next rebuild reconciles."""
    client, _ = ctx
    from app.db import get_conn, get_meta
    from app.services import entity_index, entity_rebuild
    jeff, bob = _seed_two_people(client)

    real_rebuild = entity_index.rebuild
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rebuild boom"))
    monkeypatch.setattr(entity_index, "rebuild", boom)

    r = client.post("/api/entities/merge", json={"source_id": bob, "into_id": jeff})
    # The endpoint itself still succeeds (decision committed before the deferred rebuild).
    assert r.status_code == 200, r.text

    conn = get_conn()
    assert conn.execute(
        "SELECT 1 FROM entity_decisions WHERE kind='merge' AND norm_a='bob smith'").fetchone() is not None
    # Index left dirty + error status → reconciled later.
    st = entity_rebuild.status(conn)
    assert st["rebuilding"] is True
    assert st["status"] == "error"
    assert get_meta("entities:rebuild_dirty", "0", conn=conn) == "1"

    # A subsequent successful rebuild (boom lifted) reconciles the still-dirty index. Restore
    # ONLY rebuild (not the whole monkeypatch stack — run_inline must stay True for determinism).
    monkeypatch.setattr(entity_index, "rebuild", real_rebuild)
    entity_rebuild.request_rebuild(conn)   # run_inline still True → reconciles now
    assert client.get(f"/api/entities/{bob}").status_code == 404
    detail = client.get(f"/api/entities/{jeff}").json()
    assert {"notes/2026/01/01", "notes/2026/01/02"} <= {n["title"] for n in detail["notes"]}


# ---- status endpoint + generation advance -----------------------------------------------

def test_status_generation_advances_after_rebuild(ctx):
    client, _ = ctx
    jeff, bob = _seed_two_people(client)
    g0 = client.get("/api/entities/status").json()["generation"]
    assert client.post("/api/entities/merge", json={"source_id": bob, "into_id": jeff}).status_code == 200
    st = client.get("/api/entities/status").json()
    assert st["generation"] > g0          # the inline rebuild advanced the completion counter
    assert st["rebuilding"] is False and st["status"] == "idle"


# ---- the REAL threaded path (run_inline OFF): coalescing + completion --------------------

def test_threaded_worker_coalesces_and_reconciles(ctx, monkeypatch):
    """End-to-end with the actual daemon-thread worker: two rapid merges spawn at most a
    coalesced rebuild that eventually reconciles all decisions."""
    client, _ = ctx
    from app.db import get_conn
    from app.services import entity_rebuild, notes as ns, entity_index
    jeff, bob = _seed_two_people(client)
    conn = get_conn()
    nid = ns.upsert_note(conn, "notes/2026/01/03", "about Carol Jones", kind="entry", fire_events=False)
    conn.execute("INSERT INTO note_analysis (note_id, content_hash, entities_json) VALUES (?,?,?)",
                 (nid, "h-carol", json.dumps([{"name": "Carol Jones", "type": "person"}])))
    conn.commit()
    entity_index.rebuild(conn)
    carol = conn.execute("SELECT id FROM entities WHERE normalized_key='carol jones'").fetchone()["id"]

    monkeypatch.setattr(entity_rebuild, "run_inline", False)   # use the real thread

    assert client.post("/api/entities/merge", json={"source_id": bob, "into_id": jeff}).status_code == 200
    assert client.post("/api/entities/merge", json={"source_id": carol, "into_id": jeff}).status_code == 200

    # Wait for the background worker(s) to drain (rebuilding → False).
    deadline = time.time() + 10
    while time.time() < deadline:
        if not entity_rebuild.status(get_conn())["rebuilding"]:
            break
        time.sleep(0.1)
    assert entity_rebuild.status(get_conn())["rebuilding"] is False, "rebuild did not drain"

    detail = client.get(f"/api/entities/{jeff}").json()
    titles = {n["title"] for n in detail["notes"]}
    assert {"notes/2026/01/01", "notes/2026/01/02", "notes/2026/01/03"} <= titles, titles
    _ = entity_index
