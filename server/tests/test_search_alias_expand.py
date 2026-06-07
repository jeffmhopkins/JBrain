"""Search alias-expansion — invariants the red-team review flagged as untested.

`search.hybrid_notes(entity_expand=True)` is the OWNER-CONTEXT channel that lets a query
for an alias ("Jeff Hopkins") reach notes filed under the canonical entity ("Jeffrey
Hopkins") even when the alias never appears in the note body. These tests lock in that the
expansion (a) does NOT outrank a genuinely strong FTS body match (over-expansion guard),
(b) is strictly OPT-IN (default off — the path used by _tool_reference_lookup), and
(c) refuses to blend an AMBIGUOUS alias.

Embeddings + the entity-embedding sync are monkeypatched so the suite runs without the
model (mirrors test_alias_linking.py).
"""
import inspect
import os
import tempfile

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")


@pytest.fixture()
def conn(monkeypatch):
    tmp = tempfile.mkdtemp()
    os.environ.update(
        DB_PATH=os.path.join(tmp, "test.db"),
        JBRAIN_ACCESS_KEY="test-access-key-1234567890",
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
    monkeypatch.setattr(entity_index, "_sync_embeddings", lambda *a, **k: None)

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    c = db.get_conn()
    db.ensure_default_person(c)
    return c


# ---- helpers ----------------------------------------------------------------------------

def _mk(conn, title, body="x", kind="entry"):
    from app.services import notes as notes_svc
    notes_svc.upsert_note(conn, title, body, kind=kind, fire_events=False)
    conn.commit()
    return notes_svc.get_by_title(conn, title)


def _entity(conn, name, article_title=None, etype="person", aliases=()):
    from app.services import entity_index
    nk = entity_index.normalize(name)
    conn.execute("INSERT INTO entities (type, canonical_name, normalized_key, article_title) VALUES (?,?,?,?)",
                 (etype, name, nk, article_title))
    eid = conn.execute("SELECT id FROM entities WHERE type=? AND normalized_key=?", (etype, nk)).fetchone()["id"]
    for a in aliases:
        conn.execute("INSERT OR IGNORE INTO entity_aliases (entity_id, alias_norm, alias_display) VALUES (?,?,?)",
                     (eid, entity_index.normalize(a), a))
    conn.commit()
    return eid


def _fts_has(conn, query: str) -> bool:
    """Does notes_fts return anything at all for `query` in this env? (Tells us whether the
    'strong FTS hit ranks first' assertion is exercisable or we fall back to the weaker one.)"""
    from app.services.search import _fts_query
    try:
        return bool(conn.execute(
            "SELECT 1 FROM notes_fts WHERE notes_fts MATCH ? LIMIT 1", (_fts_query(query),)
        ).fetchone())
    except Exception:  # noqa: BLE001
        return False


# ---- A. over-expansion ranking ----------------------------------------------------------

def test_entity_expand_does_not_outrank_strong_fts_body_match(conn):
    """A note whose BODY strongly contains the query must stay at/near the top; an
    entity-mention-only note (no body match, reached only via the alias channel) must NOT
    leapfrog it. Locks in that the expansion is a 'modest fixed-rank bump', not a dominator."""
    from app.services import search
    strong = _mk(conn, "notes/strong", "Jeff Hopkins Jeff Hopkins meeting notes", kind="entry")
    weak = _mk(conn, "notes/weak", "had lunch downtown today", kind="entry")  # no name in body
    eid = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (eid, weak["id"]))
    conn.commit()

    exp = search.hybrid_notes(conn, "Jeff Hopkins", 8, entity_expand=True)
    ids = [r["id"] for r in exp]
    assert weak["id"] in ids, "entity-only note should surface under entity_expand"

    if _fts_has(conn, "Jeff Hopkins"):
        # Full invariant: the strong body match wins outright.
        assert ids[0] == strong["id"], f"strong FTS note must rank #1, got order {ids}"
        assert ids.index(strong["id"]) < ids.index(weak["id"])
    else:
        # FTS unavailable in this env: assert the weaker invariant — the entity-only note is
        # present but the entity channel alone (rank-2 bump) doesn't dominate a real match.
        if strong["id"] in ids:
            assert ids.index(strong["id"]) <= ids.index(weak["id"])


# ---- B. negative scope (opt-in only) ----------------------------------------------------

def test_default_does_not_surface_entity_only_note(conn):
    """hybrid_notes WITHOUT entity_expand (the default) must NOT surface the entity-only
    note for an alias query — expansion is opt-in. This is the path _tool_reference_lookup
    uses (it calls hybrid_notes with no entity_expand)."""
    from app.services import search
    weak = _mk(conn, "notes/ref-only", "had lunch downtown today", kind="entry")
    eid = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (eid, weak["id"]))
    conn.commit()

    base = search.hybrid_notes(conn, "Jeff Hopkins", 8)  # default entity_expand=False
    assert all(r["id"] != weak["id"] for r in base), "default path must not blend entity notes"


def test_hybrid_notes_default_entity_expand_is_false():
    """The signature default is entity_expand=False — so any caller that omits it (e.g.
    _tool_reference_lookup) gets the alias-blind, scope-safe behavior. Asserting the default
    directly is the cleanest proxy for 'reference_lookup does not expand'."""
    from app.services import search
    sig = inspect.signature(search.hybrid_notes)
    assert sig.parameters["entity_expand"].default is False


def test_reference_lookup_calls_hybrid_without_entity_expand(conn, monkeypatch):
    """Behavioral confirmation: architect._tool_reference_lookup invokes hybrid_notes
    WITHOUT entity_expand=True (it must stay scope-safe). Capture the kwargs it passes."""
    from app.services import architect, search
    _mk(conn, "kb/Reference/Medicine/Asthma", "Asthma is a condition.", kind="kb")
    captured = {}

    def fake_hybrid(c, q, limit=8, *, entity_expand=False, **kwargs):
        captured["entity_expand"] = entity_expand
        return []

    monkeypatch.setattr(search, "hybrid_notes", fake_hybrid)
    architect._tool_reference_lookup(conn, "asthma", 6)
    assert captured.get("entity_expand", False) is False


# ---- C. ambiguity ------------------------------------------------------------------------

def test_entity_expand_skips_ambiguous_alias_no_blend(conn):
    """An alias ("Jeff") mapping to two distinct entities is ambiguous → entity_expand must
    NOT blend EITHER corpus. (Partly covered in test_alias_linking; this re-asserts at the
    search layer with the alias on both entities so ambiguous_terms catches the alias norm.)"""
    from app.services import search
    na = _mk(conn, "notes/amb-a", "lunch a", kind="entry")
    nb = _mk(conn, "notes/amb-b", "lunch b", kind="entry")
    ea = _entity(conn, "Jeff Hopkins", aliases=["Jeff"])
    eb = _entity(conn, "Jeff Stevens", aliases=["Jeff"])
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (ea, na["id"]))
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (eb, nb["id"]))
    conn.commit()
    exp = search.hybrid_notes(conn, "Jeff", 8, entity_expand=True)
    assert all(r["id"] not in (na["id"], nb["id"]) for r in exp), "ambiguous alias must not blend"


def test_entity_expand_skips_name_ambiguous_across_two_entities(conn):
    """A name ('jeff') that resolves to two distinct entities — one via its key, one via an
    alias — collides in ambiguous_terms and must not expand either corpus."""
    from app.services import search
    na = _mk(conn, "notes/dup-a", "note a", kind="entry")
    nb = _mk(conn, "notes/dup-b", "note b", kind="entry")
    ea = _entity(conn, "Jeff", aliases=())              # canonical key 'jeff'
    eb = _entity(conn, "Jeffery Smith", aliases=["Jeff"])  # alias 'jeff' -> a different entity
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (ea, na["id"]))
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (eb, nb["id"]))
    conn.commit()
    # 'jeff' now maps to entity ea (key) AND eb (alias) → ambiguous → no blend.
    exp = search.hybrid_notes(conn, "Jeff", 8, entity_expand=True)
    assert all(r["id"] not in (na["id"], nb["id"]) for r in exp)
