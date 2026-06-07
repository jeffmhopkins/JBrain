"""Identity-reconciliation HTTP endpoints — /api/entities merge / split / aliases / decisions.

Drives the REAL FastAPI routes via a TestClient (auth = the access key, like test_api /
test_redirects). merge/split/aliases each call entity_index.rebuild(), so we monkeypatch
embeddings AND entity_index._sync_embeddings (which pulls fastembed) to no-ops in the
fixture, mirroring test_alias_linking.py.

Two distinct person entities are seeded from 'entry' notes whose analysis names them
("Jeffrey Hopkins", "Bob Smith"), each with a kb article, then entity_index.rebuild().
"""
import json
import os
import tempfile

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")
pytest.importorskip("anthropic")

TEST_KEY = "test-access-key-1234567890"


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    os.environ.update(
        DB_PATH=os.path.join(tmp, "test.db"),
        JBRAIN_ACCESS_KEY=TEST_KEY,
        BRAIN_NAME="Test Brain",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()

    from app.services import embeddings, entity_index
    for name in ("upsert_note_embedding", "delete_note_embedding", "upsert_attachment_embeddings",
                 "delete_attachment_embeddings", "delete_entity_embedding"):
        monkeypatch.setattr(embeddings, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "semantic_search_attachments", lambda *a, **k: [])
    # merge/split/aliases all call entity_index.rebuild(), which runs _sync_embeddings
    # (fastembed). No-op it so the endpoints run offline.
    monkeypatch.setattr(entity_index, "_sync_embeddings", lambda *a, **k: None)

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()

    from app import auth
    auth.ensure_access_key()

    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})


def _seed_two_people(client):
    """Create two analyzed 'entry' notes + kb articles, then build the entity index.
    Returns (jeff_entity_id, bob_entity_id)."""
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


# ---- merge ------------------------------------------------------------------------------

def test_merge_folds_source_into_survivor(client):
    """POST /api/entities/merge {source_id, into_id} → 200 returning the survivor detail;
    afterward the source entity is gone and its notes are folded under the survivor."""
    jeff, bob = _seed_two_people(client)
    r = client.post("/api/entities/merge", json={"source_id": bob, "into_id": jeff})
    assert r.status_code == 200, r.text
    survivor = r.json()
    assert survivor["id"] == jeff
    assert survivor["normalized_key"] == "jeffrey hopkins"

    # The source entity id no longer resolves; its notes now live under the survivor.
    assert client.get(f"/api/entities/{bob}").status_code == 404
    detail = client.get(f"/api/entities/{jeff}").json()
    titles = {n["title"] for n in detail["notes"]}
    assert {"notes/2026/01/01", "notes/2026/01/02"} <= titles, f"both source notes folded in, got {titles}"


def test_merge_into_itself_400(client):
    """Merging an entity into itself is refused with 400."""
    jeff, _ = _seed_two_people(client)
    r = client.post("/api/entities/merge", json={"source_id": jeff, "into_id": jeff})
    assert r.status_code == 400, r.text


# ---- aliases ----------------------------------------------------------------------------

def test_add_list_and_delete_alias(client):
    """POST aliases → the alias appears in the detail; GET decisions lists the alias ruling;
    DELETE removes it (and the alias drops from the detail after rebuild)."""
    from app.services import entity_index
    jeff, _ = _seed_two_people(client)

    r = client.post(f"/api/entities/{jeff}/aliases", json={"display": "Jeff Hopkins"})
    assert r.status_code == 200, r.text
    assert "Jeff Hopkins" in r.json()["aliases"]

    decisions = client.get(f"/api/entities/{jeff}/decisions").json()
    alias_rows = [d for d in decisions if d["kind"] == "alias"]
    assert any(d["norm_a"] == "jeff hopkins" and d["norm_b"] == "jeffrey hopkins" for d in alias_rows), decisions

    alias_norm = entity_index.normalize("Jeff Hopkins")
    d = client.delete(f"/api/entities/{jeff}/aliases/{alias_norm}")
    assert d.status_code == 200, d.text
    after = client.get(f"/api/entities/{jeff}").json()
    assert "Jeff Hopkins" not in after["aliases"]
    decisions2 = client.get(f"/api/entities/{jeff}/decisions").json()
    assert not any(dd["kind"] == "alias" and dd["norm_a"] == alias_norm for dd in decisions2)


def test_add_alias_blank_rejected(client):
    """A blank alias display is rejected (422)."""
    jeff, _ = _seed_two_people(client)
    r = client.post(f"/api/entities/{jeff}/aliases", json={"display": "   "})
    assert r.status_code == 422, r.text


# ---- split ------------------------------------------------------------------------------

def test_split_records_decision(client):
    """POST /api/entities/split {a_id, b_id} → 200; a 'split' decision is recorded and shows
    in list_decisions for the pair."""
    jeff, bob = _seed_two_people(client)
    r = client.post("/api/entities/split", json={"a_id": jeff, "b_id": bob})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True

    decisions = client.get(f"/api/entities/{jeff}/decisions").json()
    splits = [d for d in decisions if d["kind"] == "split"]
    assert any({d["norm_a"], d["norm_b"]} == {"jeffrey hopkins", "bob smith"} for d in splits), decisions


def test_split_into_itself_400(client):
    """Splitting an entity from itself is refused with 400."""
    jeff, _ = _seed_two_people(client)
    r = client.post("/api/entities/split", json={"a_id": jeff, "b_id": jeff})
    assert r.status_code == 400, r.text


# ---- auth gate --------------------------------------------------------------------------

def test_endpoints_require_auth(client):
    """The /api/entities reconciliation routes are auth-gated (no key → 401)."""
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    assert anon.post("/api/entities/merge", json={"source_id": 1, "into_id": 2}).status_code == 401
