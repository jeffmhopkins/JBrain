"""Per-note governance flags: `kb_ingest` (feed KB synthesis) and `tool_access`
(visible to the assistant's search/research tools).

These lock the invariants the design panel + red-team flagged:

  * Migration v56 adds both columns defaulting to 1 (today's behaviour) and backfills
    EXISTING saved guest-chats (notes/chats/…) to Research-only (kb_ingest=0).
  * kb_ingest=0 removes an entry from EVERY KB-source chokepoint (the 6 verified sites
    + the semantic context gather) — a leak-free guarantee, not just one query.
  * tool_access=0 hides an entry from the assistant's retrieval path (hybrid_notes
    post-filter + semantic_search inline gate) WITHOUT touching owner-facing search
    (default-off gates).
  * Saved chats default to Research-only at write time (chat_share.save_to_brain).
  * The flags endpoint enforces the three coherent presets and re-arms synthesis on
    re-enable.

Embeddings are stubbed for the SQL/FTS suites (the `conn` fixture); the few inline
semantic_search gate tests use the real model via the `rconn` fixture.
"""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")

import app.db as db

SCHEMA = Path(db.__file__).parent / "schema.sql"
TEST_KEY = "test-access-key-1234567890"


def _fresh_env(tmp):
    os.environ.update(
        DB_PATH=os.path.join(tmp, "test.db"),
        JBRAIN_ACCESS_KEY=TEST_KEY,
        BRAIN_NAME="Test Brain",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()


@pytest.fixture()
def conn(monkeypatch):
    """Initialised DB with the embedding/FTS-embedding layer stubbed out (fast, no model)."""
    tmp = tempfile.mkdtemp()
    _fresh_env(tmp)
    from app.services import embeddings, entity_index
    for name in ("upsert_note_embedding", "delete_note_embedding", "upsert_attachment_embeddings",
                 "delete_attachment_embeddings", "delete_entity_embedding"):
        monkeypatch.setattr(embeddings, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "semantic_search_attachments", lambda *a, **k: [])
    monkeypatch.setattr(entity_index, "_sync_embeddings", lambda *a, **k: None, raising=False)
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    c = db.get_conn()
    db.ensure_default_person(c)
    return c


@pytest.fixture()
def rconn(monkeypatch):
    """Initialised DB with REAL embeddings — for the semantic_search inline-gate tests."""
    tmp = tempfile.mkdtemp()
    _fresh_env(tmp)
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    c = db.get_conn()
    db.ensure_default_person(c)
    return c


def _mk(conn, title, body="x", kind="entry", *, kb_ingest=1, tool_access=1):
    from app.services import notes as notes_svc
    notes_svc.upsert_note(conn, title, body, kind=kind, fire_events=False)
    row = notes_svc.get_by_title(conn, title)
    conn.execute("UPDATE notes SET kb_ingest=?, tool_access=? WHERE id=?",
                 (kb_ingest, tool_access, row["id"]))
    conn.commit()
    return notes_svc.get_by_title(conn, title)


def _ctx(conn):
    from app.services.pipeline import _Ctx
    return _Ctx(conn, None, {})


# --------------------------------------------------------------------------------------
# 1. Schema + migration
# --------------------------------------------------------------------------------------

def _has_col(conn, table, col):
    return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def test_fresh_db_has_flags_defaulting_on(conn):
    n = _mk(conn, "notes/Hello", "hi")
    row = conn.execute("SELECT kb_ingest, tool_access FROM notes WHERE id=?", (n["id"],)).fetchone()
    assert (row["kb_ingest"], row["tool_access"]) == (1, 1)


def test_migration_v56_adds_columns_and_backfills_chats(monkeypatch):
    """A pre-v56 DB (no flag columns) upgrades cleanly: every row defaults to 1/1, and any
    EXISTING saved guest-chat (notes/chats/…) is demoted to Research-only (kb_ingest=0)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    # Roll the notes table back to its pre-v56 shape.
    c.execute("ALTER TABLE notes DROP COLUMN kb_ingest")
    c.execute("ALTER TABLE notes DROP COLUMN tool_access")
    db.set_meta(c, "schema_version", "55")
    # A normal entry and a previously-saved guest chat, both pre-migration.
    c.execute("INSERT INTO notes (slug, title, content_md, kind) VALUES ('a','notes/Plain','b','entry')")
    c.execute("INSERT INTO notes (slug, title, content_md, kind) "
              "VALUES ('c','notes/chats/Chat with Sam — 2026-06-07','x','entry')")
    c.commit()
    monkeypatch.setattr(db, "get_conn", lambda: c)   # get_meta/set_meta target this conn
    db._run_migrations(c)
    assert _has_col(c, "notes", "kb_ingest") and _has_col(c, "notes", "tool_access")
    plain = c.execute("SELECT kb_ingest, tool_access FROM notes WHERE slug='a'").fetchone()
    chat = c.execute("SELECT kb_ingest, tool_access FROM notes WHERE slug='c'").fetchone()
    assert (plain["kb_ingest"], plain["tool_access"]) == (1, 1)          # untouched
    assert (chat["kb_ingest"], chat["tool_access"]) == (0, 1)            # → research-only


# --------------------------------------------------------------------------------------
# 2. KB ingestion chokepoints — kb_ingest=0 must NOT be a source anywhere
# --------------------------------------------------------------------------------------

def test_query_entry_changes_excludes_kb_ingest_zero(conn):
    keep = _mk(conn, "notes/Keep", "alpha", kb_ingest=1)
    drop = _mk(conn, "notes/Drop", "beta", kb_ingest=0)
    from app.services import pipeline
    ids = {r["id"] for r in pipeline._p_query_entry_changes(_ctx(conn), since="", limit=50)}
    assert keep["id"] in ids
    assert drop["id"] not in ids


def test_kb_uncited_pending_excludes_kb_ingest_zero(conn):
    keep = _mk(conn, "notes/Keep", "alpha", kb_ingest=1)
    drop = _mk(conn, "notes/Drop", "beta", kb_ingest=0)
    from app.services import pipeline
    out = pipeline._p_kb_uncited_pending(_ctx(conn), limit=50)
    assert keep["id"] in out["ids"]
    assert drop["id"] not in out["ids"]


def test_chatter_pending_excludes_kb_ingest_zero(conn):
    keep = _mk(conn, "notes/Keep", "alpha", kb_ingest=1)
    drop = _mk(conn, "notes/Drop", "beta", kb_ingest=0)
    from app.services import pipeline
    ids = {e["id"] for e in pipeline._p_chatter_pending(_ctx(conn), limit=500)["entries"]}
    assert keep["id"] in ids
    assert drop["id"] not in ids


def test_corpus_digest_excludes_kb_ingest_zero(conn):
    keep = _mk(conn, "notes/Keep", "alpha", kb_ingest=1)
    drop = _mk(conn, "notes/Drop", "beta", kb_ingest=0)
    from app.services import wiki_build
    ids = {r["id"] for r in wiki_build.corpus_digest(conn, limit=3000)}
    assert keep["id"] in ids
    assert drop["id"] not in ids


def test_entity_index_collect_excludes_kb_ingest_zero(conn):
    keep = _mk(conn, "notes/Keep", "alpha", kb_ingest=1)
    drop = _mk(conn, "notes/Drop", "beta", kb_ingest=0)
    ents = json.dumps([{"name": "Acme Corporation", "type": "org"}])
    for nid in (keep["id"], drop["id"]):
        conn.execute("INSERT INTO note_analysis (note_id, content_hash, entities_json) VALUES (?,?,?)",
                     (nid, "h", ents))
    conn.commit()
    from app.services import entity_index
    clusters = entity_index._collect(conn, limit=1000)
    note_ids = set()
    for byname in clusters.values():
        for c in byname.values():
            note_ids |= c["notes"]
    assert keep["id"] in note_ids
    assert drop["id"] not in note_ids


def test_update_batch_change_query_excludes_kb_ingest_zero(conn, monkeypatch):
    """The incremental-update change feed skips kb_ingest=0 notes. A new kb_ingest=1 orphan
    (no citations, no entities) flows through with NO LLM call — the positive control."""
    from app.services import wiki_build, llm
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    db.set_meta(conn, wiki_build._WATERMARK, "2000-01-01 00:00:00")
    conn.commit()
    _mk(conn, "notes/Drop", "beta", kb_ingest=0)
    assert wiki_build.update_batch(conn)["changes"] == 0          # opted-out → invisible

    _mk(conn, "notes/Keep", "alpha", kb_ingest=1)
    db.set_meta(conn, wiki_build._WATERMARK, "2000-01-01 00:00:00")
    conn.commit()
    assert wiki_build.update_batch(conn)["changes"] == 1          # opted-in → seen


def test_gather_context_passes_kb_ingest_gate(conn, monkeypatch):
    """_p_gather_context (KB synthesis context) must call semantic_search with the kb_ingest
    gate, so a research-only note can't leak into synthesis prose."""
    from app.services import pipeline, embeddings
    captured = {}

    def fake(c, q, limit=10, *, require_tool_access=False, require_kb_ingest=False):
        captured["kb"] = require_kb_ingest
        return []

    monkeypatch.setattr(embeddings, "semantic_search", fake)
    pipeline._p_gather_context(_ctx(conn), context_query="anything")
    assert captured.get("kb") is True


# --------------------------------------------------------------------------------------
# 3. Tool-access gating — assistant retrieval hides tool_access=0; owner search doesn't
# --------------------------------------------------------------------------------------

def test_hybrid_notes_post_filter_gates_tool_access(conn):
    """hybrid_notes(require_tool_access=True) drops a tool_access=0 note from FTS results,
    while the default (owner-facing) call still returns it."""
    from app.services import search
    keep = _mk(conn, "notes/Visible", "kangaroo facts", tool_access=1)
    hide = _mk(conn, "notes/Hidden", "kangaroo facts", tool_access=0)
    default = {r["id"] for r in search.hybrid_notes(conn, "kangaroo", 8)}
    gated = {r["id"] for r in search.hybrid_notes(conn, "kangaroo", 8, require_tool_access=True)}
    assert keep["id"] in default and hide["id"] in default        # owner sees both
    assert keep["id"] in gated and hide["id"] not in gated        # assistant doesn't


def test_hybrid_notes_post_filter_gates_kb_ingest(conn):
    from app.services import search
    keep = _mk(conn, "notes/Src", "platypus facts", kb_ingest=1)
    drop = _mk(conn, "notes/NoSrc", "platypus facts", kb_ingest=0)
    gated = {r["id"] for r in search.hybrid_notes(conn, "platypus", 8, require_kb_ingest=True)}
    assert keep["id"] in gated and drop["id"] not in gated


def test_architect_search_notes_hides_tool_access_zero(conn):
    """End-to-end through the assistant tool: a Private note never surfaces in search_notes/find."""
    from app.services import architect
    _mk(conn, "notes/Secret", "wombat ledger", tool_access=0)
    _mk(conn, "notes/Open", "wombat ledger", tool_access=1)
    for out in (architect._tool_search_notes(conn, "wombat", 8),
                architect._tool_find(conn, "wombat", 6)):
        assert "Open" in out
        assert "Secret" not in out


def test_semantic_search_inline_tool_access_gate(rconn):
    """semantic_search(require_tool_access=True) excludes a tool_access=0 note; default keeps it."""
    from app.services import embeddings
    keep = _mk(rconn, "notes/SemKeep", "the quick brown fox jumps", tool_access=1)
    hide = _mk(rconn, "notes/SemHide", "the quick brown fox jumps", tool_access=0)
    default = {r["id"] for r in embeddings.semantic_search(rconn, "quick brown fox", 10)}
    gated = {r["id"] for r in embeddings.semantic_search(
        rconn, "quick brown fox", 10, require_tool_access=True)}
    assert keep["id"] in default and hide["id"] in default
    assert keep["id"] in gated and hide["id"] not in gated


def test_semantic_search_inline_kb_ingest_gate(rconn):
    from app.services import embeddings
    keep = _mk(rconn, "notes/KbKeep", "saturn has many rings", kb_ingest=1)
    drop = _mk(rconn, "notes/KbDrop", "saturn has many rings", kb_ingest=0)
    gated = {r["id"] for r in embeddings.semantic_search(
        rconn, "saturn rings", 10, require_kb_ingest=True)}
    assert keep["id"] in gated and drop["id"] not in gated


# --------------------------------------------------------------------------------------
# 4. Saved-chat write default
# --------------------------------------------------------------------------------------

def test_saved_chat_defaults_to_research_only(conn):
    from app.services import chat_share as cs
    _, link_id = cs.create_channel(
        conn, owner_wrap="O", guest_wrap="G", persist=True, otp_required=False,
        label="x", ttl_days=7, single_use=False)
    conn.commit()
    out = cs.save_to_brain(conn, link_id, transcript_md="hello world",
                           title="Chat with Sam", attachments=[], guest_name="Sam")
    row = conn.execute("SELECT kind, kb_ingest, tool_access FROM notes WHERE slug=?",
                       (out["note_slug"],)).fetchone()
    assert row["kind"] == "entry"
    assert (row["kb_ingest"], row["tool_access"]) == (0, 1)      # research-only


# --------------------------------------------------------------------------------------
# 5. The flags API
# --------------------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    _fresh_env(tmp)
    from app.services import embeddings
    for name in ("upsert_note_embedding", "delete_note_embedding", "upsert_attachment_embeddings",
                 "delete_attachment_embeddings"):
        monkeypatch.setattr(embeddings, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "semantic_search_attachments", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "embed_many",
                        lambda ts: [[0.0] * embeddings.EMBEDDING_DIM for _ in ts])
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    from app import auth
    auth.ensure_access_key()
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})


def _make_note(client, title, body="x"):
    r = client.post("/api/notes", json={"title": title, "content_md": body})
    assert r.status_code == 200, r.text
    return r.json()["slug"]


def test_get_note_exposes_flags(client):
    slug = _make_note(client, "notes/Flagged")
    body = client.get(f"/api/notes/{slug}").json()
    assert body["kb_ingest"] == 1 and body["tool_access"] == 1


def test_flags_endpoint_sets_research_only(client):
    slug = _make_note(client, "notes/Demote")
    r = client.put(f"/api/notes/{slug}/flags", json={"kb_ingest": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"kb_ingest": False, "tool_access": True}
    # PATCH semantics: tool_access untouched.
    assert client.get(f"/api/notes/{slug}").json()["tool_access"] == 1


def test_flags_endpoint_private_then_full(client):
    slug = _make_note(client, "notes/Toggle")
    client.put(f"/api/notes/{slug}/flags", json={"kb_ingest": False, "tool_access": False})
    body = client.get(f"/api/notes/{slug}").json()
    assert (body["kb_ingest"], body["tool_access"]) == (0, 0)
    client.put(f"/api/notes/{slug}/flags", json={"kb_ingest": True, "tool_access": True})
    body = client.get(f"/api/notes/{slug}").json()
    assert (body["kb_ingest"], body["tool_access"]) == (1, 1)


def test_flags_endpoint_rejects_incoherent_state(client):
    """kb_ingest=1 with tool_access=0 (the KB would cite a source the assistant can't read)
    is refused — only Full / Research-only / Private are representable."""
    slug = _make_note(client, "notes/Bad")
    r = client.put(f"/api/notes/{slug}/flags", json={"kb_ingest": True, "tool_access": False})
    assert r.status_code == 422
    body = client.get(f"/api/notes/{slug}").json()
    assert (body["kb_ingest"], body["tool_access"]) == (1, 1)    # unchanged


def test_reenable_clears_evaluated_marker(client):
    """Re-enabling KB ingestion (0→1) clears the per-entry 'already evaluated' marker so the
    next coverage pass reconsiders the note instead of skipping it forever."""
    slug = _make_note(client, "notes/Reconsider")
    from app.db import get_conn, set_meta, get_meta
    conn = get_conn()
    nid = conn.execute("SELECT id FROM notes WHERE slug=?", (slug,)).fetchone()["id"]
    # Demote, then simulate synthesis having evaluated-and-skipped it.
    client.put(f"/api/notes/{slug}/flags", json={"kb_ingest": False})
    set_meta(conn, f"wiki_synth:evaluated:{nid}", "1"); conn.commit()
    # Re-enable → the marker is cleared.
    client.put(f"/api/notes/{slug}/flags", json={"kb_ingest": True})
    assert get_meta(f"wiki_synth:evaluated:{nid}") is None


def test_flags_endpoint_404_on_missing(client):
    assert client.put("/api/notes/nope-nope/flags", json={"kb_ingest": False}).status_code == 404
