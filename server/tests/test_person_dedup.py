"""Phase 5 — LLM 'same person?' proposals + the review approve/reject/undo loop.

The model is stubbed (no network); these tests pin the propose-only contract (never an
auto-merge), the candidate gating, and the durable approve/reject/undo behaviour.
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

    from app.services import embeddings
    for fn in ("upsert_note_embedding", "delete_note_embedding", "upsert_attachment_embeddings",
               "delete_attachment_embeddings", "store_entity_vector", "delete_entity_embedding"):
        monkeypatch.setattr(embeddings, fn, lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "semantic_search_attachments", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "embed_many", lambda texts: [[0.0] for _ in texts])

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()

    from app import auth
    auth.ensure_access_key()
    from app.services import share as _share_svc
    _share_svc._HITS.clear()

    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})


def _mk(conn, slug, ents):
    from app.services import notes as ns
    nid = ns.upsert_note(conn, slug, "x")
    conn.execute("INSERT INTO note_analysis (note_id, content_hash, entities_json) VALUES (?,?,?)",
                 (nid, slug, json.dumps(ents)))
    return nid


def _stub_llm(monkeypatch, same=True, confidence=0.9, why="nickname"):
    from app.services import llm
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    monkeypatch.setattr(llm, "complete",
                        lambda *a, **k: json.dumps({"same": same, "confidence": confidence, "why": why}))


def _persons(conn):
    return [r["canonical_name"] for r in conn.execute(
        "SELECT canonical_name FROM entities WHERE type='person' ORDER BY canonical_name")]


def test_propose_posts_card_and_never_auto_merges(client, monkeypatch):
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    _mk(conn, "n2", [{"type": "person", "name": "J Hopkins"}])      # shares surname, not auto-merged
    conn.commit()
    ei.rebuild(conn)
    assert len(_persons(conn)) == 2
    _stub_llm(monkeypatch, same=True, confidence=0.95)
    res = ei.propose_person_merges(conn)
    assert res["candidates"] >= 1 and res["proposed"] == 1
    assert len(_persons(conn)) == 2                                  # PROPOSE-ONLY: no merge happened
    cards = client.get("/api/reviews").json()
    card = next(c for c in cards if c["kind"] == "entity_merge")
    p = json.loads(card["payload_json"])
    assert {p["source"]["norm"], p["into"]["norm"]} == {"jeffrey hopkins", "j hopkins"}


def test_propose_skips_different_surnames_and_negative_verdicts(client, monkeypatch):
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    _mk(conn, "n2", [{"type": "person", "name": "Maria Delgado"}])   # different surname → not a candidate
    conn.commit()
    ei.rebuild(conn)
    _stub_llm(monkeypatch, same=True)
    assert ei.propose_person_merges(conn)["candidates"] == 0

    # Same surname but the model says DIFFERENT → no card.
    _mk(conn, "n3", [{"type": "person", "name": "John Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    _stub_llm(monkeypatch, same=False)
    assert ei.propose_person_merges(conn)["proposed"] == 0


def test_approve_records_durable_merge_and_survives_rebuild(client, monkeypatch):
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    _mk(conn, "n2", [{"type": "person", "name": "J Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    _stub_llm(monkeypatch)
    ei.propose_person_merges(conn)
    card = next(c for c in client.get("/api/reviews").json() if c["kind"] == "entity_merge")

    r = client.post(f"/api/reviews/{card['id']}/approve")
    assert r.status_code == 200, r.text
    assert len(_persons(conn)) == 1                                  # merged
    ei.rebuild(conn)
    assert len(_persons(conn)) == 1                                  # durable across rebuild
    # The card is dismissed and carries the decision id for undo.
    assert not any(c["id"] == card["id"] for c in client.get("/api/reviews").json())
    assert r.json()["decision_id"]


def test_undo_reverts_an_approved_merge(client, monkeypatch):
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    _mk(conn, "n2", [{"type": "person", "name": "J Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    _stub_llm(monkeypatch)
    ei.propose_person_merges(conn)
    cid = next(c for c in client.get("/api/reviews").json() if c["kind"] == "entity_merge")["id"]
    client.post(f"/api/reviews/{cid}/approve")
    assert len(_persons(conn)) == 1
    # Undo works off the dismissed card (status=dismissed list).
    r = client.post(f"/api/reviews/{cid}/undo")
    assert r.status_code == 200, r.text
    ei.rebuild(conn)
    assert len(_persons(conn)) == 2                                  # split back apart


def test_reject_records_split_and_suppresses_reproposal(client, monkeypatch):
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    _mk(conn, "n2", [{"type": "person", "name": "J Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    _stub_llm(monkeypatch)
    ei.propose_person_merges(conn)
    cid = next(c for c in client.get("/api/reviews").json() if c["kind"] == "entity_merge")["id"]

    assert client.post(f"/api/reviews/{cid}/reject").status_code == 200
    assert frozenset({"jeffrey hopkins", "j hopkins"}) in ed.load_splits(conn, "person")
    # A second proposer run must NOT re-propose the rejected pair.
    assert ei.propose_person_merges(conn)["proposed"] == 0


def test_open_proposal_is_not_reproposed(client, monkeypatch):
    """A pending card for a pair suppresses a duplicate card on the next run."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    _mk(conn, "n2", [{"type": "person", "name": "J Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    _stub_llm(monkeypatch)
    assert ei.propose_person_merges(conn)["proposed"] == 1
    assert ei.propose_person_merges(conn)["proposed"] == 0           # already open → not re-proposed


def test_no_llm_credentials_means_no_proposals(client, monkeypatch):
    from app.db import get_conn
    from app.services import entity_index as ei, llm
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    _mk(conn, "n2", [{"type": "person", "name": "J Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    monkeypatch.setattr(llm, "has_credentials", lambda: False)
    assert ei.propose_person_merges(conn)["proposed"] == 0


def test_propose_verb_registered():
    from app.services import pipeline
    assert "propose_person_merges" in pipeline._PRIMITIVES
    assert "propose_person_merges" in pipeline._PRIMITIVE_META


def _propose_one(client, conn, monkeypatch):
    """Helper: two same-surname people + a high-confidence proposal → the card id."""
    from app.services import entity_index as ei
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    _mk(conn, "n2", [{"type": "person", "name": "J Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    _stub_llm(monkeypatch)
    ei.propose_person_merges(conn)
    return next(c for c in client.get("/api/reviews").json() if c["kind"] == "entity_merge")["id"]


def test_double_approve_is_rejected(client, monkeypatch):
    from app.db import get_conn
    conn = get_conn()
    cid = _propose_one(client, conn, monkeypatch)
    assert client.post(f"/api/reviews/{cid}/approve").status_code == 200
    assert client.post(f"/api/reviews/{cid}/approve").status_code == 409   # already resolved


def test_reject_after_approve_is_rejected(client, monkeypatch):
    from app.db import get_conn
    conn = get_conn()
    cid = _propose_one(client, conn, monkeypatch)
    assert client.post(f"/api/reviews/{cid}/approve").status_code == 200
    assert client.post(f"/api/reviews/{cid}/reject").status_code == 409    # can't reject a resolved card


def test_undo_twice_is_rejected(client, monkeypatch):
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    cid = _propose_one(client, conn, monkeypatch)
    client.post(f"/api/reviews/{cid}/approve")
    assert client.post(f"/api/reviews/{cid}/undo").status_code == 200
    assert len(_persons(conn)) == 2
    # decision_id was cleared → a second undo has nothing to act on.
    assert client.post(f"/api/reviews/{cid}/undo").status_code == 400


def test_approve_keeps_card_pending_when_article_fold_fails(client, monkeypatch):
    """If the (LLM) article fold fails, the entity merge still applies but the card stays
    PENDING with the reason — no false success, retry affordance preserved."""
    from app.db import get_conn
    from app.services import entity_index as ei, wiki_build
    from app.services import notes as ns
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    _mk(conn, "n2", [{"type": "person", "name": "J Hopkins"}])
    ns.upsert_note(conn, "kb/People/Jeffrey Hopkins", "# Jeffrey Hopkins\n\nA person.", kind="kb")
    ns.upsert_note(conn, "kb/People/J Hopkins", "# J Hopkins\n\nA person.", kind="kb")
    conn.commit()
    ei.rebuild(conn)                                          # links both article_titles
    _stub_llm(monkeypatch)
    ei.propose_person_merges(conn)
    cid = next(c for c in client.get("/api/reviews").json() if c["kind"] == "entity_merge")["id"]
    # Force the article fold to fail.
    monkeypatch.setattr(wiki_build, "merge_articles", lambda *a, **k: {"ok": False, "reason": "KB busy"})
    r = client.post(f"/api/reviews/{cid}/approve")
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["merged_entities"] is True
    assert len(_persons(conn)) == 1                          # entity merge still applied
    assert any(c["id"] == cid for c in client.get("/api/reviews").json())   # card still pending (retry)
