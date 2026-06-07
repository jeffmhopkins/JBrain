"""Span-detection alias-aware search (OWNER-CONTEXT channel).

`search.hybrid_notes(entity_expand=True)` already blends a corpus when the WHOLE query is a
known name/alias. Span detection (entity_index.span_entity_note_ids) ADDS reaching a known
name MENTIONED INSIDE a sentence — "what did jeff hopkins say about taxes" should reach notes
filed under "Jeffrey" — WITHOUT over-expanding common words / bare heuristic first names,
staying owner-only (default off; same gate as the whole-query path), and never dominating a
genuine FTS body hit.

These tests lock the invariants the red-team flagged: a name in a sentence expands; multiple
names expand; a common (non-entity) word does NOT; an ambiguous name does NOT; a no-name
sentence is byte-identical to expansion OFF; a name absent from the index does NOT; ranking
(a strong FTS body hit still outranks a span-only entity hit); the per-call cap is respected;
and the negative scope — the default path and reference_lookup never span-expand.

Embeddings + the entity-embedding sync are monkeypatched so the suite runs without the model
(mirrors test_alias_linking.py / test_search_alias_expand.py).
"""
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


def _mention(conn, eid, note_id):
    conn.execute("INSERT OR IGNORE INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (eid, note_id))
    conn.commit()


def _fts_has(conn, query: str) -> bool:
    from app.services.search import _fts_query
    try:
        return bool(conn.execute(
            "SELECT 1 FROM notes_fts WHERE notes_fts MATCH ? LIMIT 1", (_fts_query(query),)
        ).fetchone())
    except Exception:  # noqa: BLE001
        return False


# ---- A. single name in a sentence expands -----------------------------------------------

def test_single_name_in_sentence_expands(conn):
    """A known multi-token name mentioned inside a sentence reaches that person's corpus,
    even though the WHOLE query is not a bare name and the body has no matching token."""
    from app.services import search
    n = _mk(conn, "notes/2026/03/01", "zzqq lunch downtown body", kind="entry")
    eid = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    _mention(conn, eid, n["id"])
    exp = search.hybrid_notes(conn, "what did jeff hopkins say about taxes", 8, entity_expand=True)
    assert any(r["id"] == n["id"] for r in exp), "span of a known name in a sentence should expand"


def test_single_name_via_canonical_key_in_sentence_expands(conn):
    """The span need not be an alias — a multi-token CANONICAL key mentioned in a sentence
    expands too (the surface set includes every entity key)."""
    from app.services import search
    n = _mk(conn, "notes/2026/03/02", "zzqq unrelated", kind="entry")
    eid = _entity(conn, "Sarah Connor")
    _mention(conn, eid, n["id"])
    exp = search.hybrid_notes(conn, "tell me what sarah connor mentioned about loans", 8, entity_expand=True)
    assert any(r["id"] == n["id"] for r in exp)


# ---- B. multiple names ------------------------------------------------------------------

def test_multiple_names_in_sentence_expand(conn):
    """Two distinct known names in one sentence each reach their corpus (up to the span cap)."""
    from app.services import search
    na = _mk(conn, "notes/2026/03/03", "zzqq aaa", kind="entry")
    nb = _mk(conn, "notes/2026/03/04", "zzqq bbb", kind="entry")
    ea = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    eb = _entity(conn, "Maria Garcia")
    _mention(conn, ea, na["id"])
    _mention(conn, eb, nb["id"])
    exp = search.hybrid_notes(conn, "did jeff hopkins and maria garcia ever meet", 8, entity_expand=True)
    ids = {r["id"] for r in exp}
    assert na["id"] in ids and nb["id"] in ids, "both named people should expand"


# ---- C. common (non-entity) word does NOT expand ----------------------------------------

def test_common_word_not_an_entity_does_not_expand(conn):
    """A common word that is NOT in the entity index must not expand anything — span detection
    is a dictionary match, so a word that's no entity surface simply never fires."""
    from app.services import search
    n = _mk(conn, "notes/2026/03/05", "zzqq private", kind="entry")
    eid = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    _mention(conn, eid, n["id"])
    # "taxes"/"lunch"/"meeting" are not entities; the named person is absent from the query.
    exp = search.hybrid_notes(conn, "what about taxes and lunch this week", 8, entity_expand=True)
    assert all(r["id"] != n["id"] for r in exp), "non-entity words must not expand a corpus"


def test_bare_heuristic_first_name_in_sentence_does_not_expand(conn):
    """A bare single-token heuristic alias ("jeff" auto-merged onto "Jeffrey") is too
    collision-prone to fire mid-sentence — dropped unless user-decided (alias_surface rule iv).
    The multi-token "jeff hopkins" WOULD expand; the lone "jeff" must not."""
    from app.services import search
    n = _mk(conn, "notes/2026/03/06", "zzqq nothing", kind="entry")
    eid = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff"])  # single-token heuristic alias
    _mention(conn, eid, n["id"])
    exp = search.hybrid_notes(conn, "what did jeff say about the trip", 8, entity_expand=True)
    assert all(r["id"] != n["id"] for r in exp), "bare heuristic first name must not span-expand"


def test_user_decided_single_name_in_sentence_expands(conn):
    """A single-token alias the USER explicitly decided (entity_decisions 'alias') IS trusted
    and expands from inside a sentence — the only single-token case that fires."""
    from app.services import search, entity_index, entity_decisions
    n = _mk(conn, "notes/2026/03/07", "zzqq nothing", kind="entry")
    eid = _entity(conn, "Jeffrey Hopkins")
    _mention(conn, eid, n["id"])
    # Register the durable user ruling that "jeff" is an alias of "jeffrey hopkins" (alias row:
    # norm_a=alias key, norm_b=canonical key, display_a=label), and put it on the entity so
    # note_ids_for_name resolves it.
    entity_decisions.add(conn, "alias", type="person", norm_a="jeff",
                         norm_b=entity_index.normalize("Jeffrey Hopkins"), display_a="Jeff")
    conn.execute("INSERT OR IGNORE INTO entity_aliases (entity_id, alias_norm, alias_display) VALUES (?,?,?)",
                 (eid, "jeff", "Jeff"))
    conn.commit()
    exp = search.hybrid_notes(conn, "what did jeff say about the trip", 8, entity_expand=True)
    assert any(r["id"] == n["id"] for r in exp), "user-decided single-name alias should span-expand"


# ---- D. ambiguous name does NOT expand --------------------------------------------------

def test_ambiguous_name_in_sentence_does_not_expand(conn):
    """A name mentioned in a sentence that maps to ≥2 distinct entities is ambiguous → it is
    removed from the span surface set and must NOT expand either corpus."""
    from app.services import search
    na = _mk(conn, "notes/2026/03/08", "zzqq a", kind="entry")
    nb = _mk(conn, "notes/2026/03/09", "zzqq b", kind="entry")
    ea = _entity(conn, "Jeff Hopkins")        # canonical key 'jeff hopkins'
    eb = _entity(conn, "Jeff Stevens", aliases=["Jeff Hopkins"])  # alias collides on same surface
    _mention(conn, ea, na["id"])
    _mention(conn, eb, nb["id"])
    exp = search.hybrid_notes(conn, "did jeff hopkins call yesterday", 8, entity_expand=True)
    assert all(r["id"] not in (na["id"], nb["id"]) for r in exp), "ambiguous span must not expand"


# ---- E. no-name sentence is byte-identical to expansion OFF -----------------------------

def test_no_name_sentence_identical_to_expand_off(conn):
    """A sentence with no in-index name must produce EXACTLY the same results with
    entity_expand on vs off — the span path is a strict no-op when nothing safe matches."""
    from app.services import search
    _mk(conn, "notes/2026/03/10", "we had pasta for dinner and watched a movie", kind="entry")
    _mk(conn, "notes/2026/03/11", "the weather was rainy all weekend", kind="entry")
    _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])  # exists but is not in the query
    q = "what did we have for dinner"
    off = search.hybrid_notes(conn, q, 8, entity_expand=False)
    on = search.hybrid_notes(conn, q, 8, entity_expand=True)
    assert [r["id"] for r in off] == [r["id"] for r in on], "no-name sentence must be identical on/off"


# ---- F. name not in the index does NOT expand -------------------------------------------

def test_name_not_in_index_does_not_expand(conn):
    """A plausible-looking multi-token name that is NOT a known entity surface does not fire —
    span detection only matches the dictionary, never a guessed name."""
    from app.services import search
    n = _mk(conn, "notes/2026/03/12", "zzqq nothing", kind="entry")
    eid = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    _mention(conn, eid, n["id"])
    exp = search.hybrid_notes(conn, "what did rebecca thornton say about it", 8, entity_expand=True)
    assert all(r["id"] != n["id"] for r in exp), "an unknown name must not expand"


# ---- G. ranking: strong FTS body hit still outranks a span-only entity hit --------------

def test_strong_fts_body_hit_outranks_span_only_entity_hit(conn):
    """A note whose BODY strongly matches the query stays at/near the top; a note reached ONLY
    via a span (no body match) must not leapfrog it — the span bump is modest, not dominant."""
    from app.services import search
    # The strong note's body contains the whole query so FTS scores it highly for the actual
    # (full-sentence) hybrid query — a genuinely strong match. notes_fts ANDs the prefix tokens,
    # so the body must carry them all to be the strong hit (not just "taxes").
    q = "what did jeff hopkins say about taxes"
    strong = _mk(conn, "notes/strong", q + " filing deductions return", kind="entry")
    span_only = _mk(conn, "notes/span", "zzqq unrelated body", kind="entry")  # only reached via the name
    eid = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    _mention(conn, eid, span_only["id"])
    exp = search.hybrid_notes(conn, q, 8, entity_expand=True)
    ids = [r["id"] for r in exp]
    assert span_only["id"] in ids, "span-only note should surface"
    if _fts_has(conn, q):
        assert ids[0] == strong["id"], f"strong FTS body note must rank #1, got {ids}"
        assert ids.index(strong["id"]) < ids.index(span_only["id"])
    elif strong["id"] in ids:
        assert ids.index(strong["id"]) <= ids.index(span_only["id"])


# ---- H. cap respected -------------------------------------------------------------------

def test_span_count_cap_respected(conn):
    """At most `max_spans` distinct named entities expand from a single query, so a query that
    crams in many names can't blow up the fusion. With three names and a cap of 2, the third
    name's corpus must not surface."""
    from app.services import search, entity_index
    na = _mk(conn, "notes/cap/a", "zzqq a", kind="entry")
    nb = _mk(conn, "notes/cap/b", "zzqq b", kind="entry")
    nc = _mk(conn, "notes/cap/c", "zzqq c", kind="entry")
    ea = _entity(conn, "Anna Adams")
    eb = _entity(conn, "Bella Brooks")
    ec = _entity(conn, "Carol Chen")
    _mention(conn, ea, na["id"])
    _mention(conn, eb, nb["id"])
    _mention(conn, ec, nc["id"])
    # Direct unit check on the bounded helper (deterministic regardless of fusion ordering).
    ids = entity_index.span_entity_note_ids(conn, "anna adams bella brooks carol chen all met", max_spans=2)
    surfaced = {na["id"], nb["id"], nc["id"]} & set(ids)
    assert nc["id"] not in surfaced, "third name beyond the span cap must not expand"
    assert na["id"] in ids and nb["id"] in ids, "first two names within the cap should expand"


def test_span_note_cap_respected(conn):
    """The helper caps the total number of blended note ids (max_notes) so one prolific person
    can't flood the fusion."""
    from app.services import search, entity_index
    eid = _entity(conn, "Prolific Person")
    for k in range(10):
        nk = _mk(conn, f"notes/many/{k}", "zzqq", kind="entry")
        _mention(conn, eid, nk["id"])
    ids = entity_index.span_entity_note_ids(conn, "what did prolific person do", max_notes=3)
    assert len(ids) <= 3, "blended note ids must be capped by max_notes"


# ---- I. negative scope: default path & reference_lookup never span-expand ---------------

def test_default_path_does_not_span_expand(conn):
    """hybrid_notes WITHOUT entity_expand (the default — reference_lookup / gather / rebuild)
    must NOT span-expand a name found inside a sentence. Span detection lives entirely under
    the owner-only entity_expand gate."""
    from app.services import search
    n = _mk(conn, "notes/2026/03/20", "zzqq nothing", kind="entry")
    eid = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    _mention(conn, eid, n["id"])
    base = search.hybrid_notes(conn, "what did jeff hopkins say about taxes", 8)  # default off
    assert all(r["id"] != n["id"] for r in base), "default path must not span-expand"


def test_reference_lookup_does_not_span_expand(conn, monkeypatch):
    """architect._tool_reference_lookup calls hybrid_notes WITHOUT entity_expand=True, so it
    can never span-expand — capture the kwargs it passes."""
    from app.services import architect, search
    _mk(conn, "kb/Reference/Medicine/Asthma", "Asthma is a condition.", kind="kb")
    captured = {}

    def fake_hybrid(c, q, limit=8, *, entity_expand=False):
        captured["entity_expand"] = entity_expand
        return []

    monkeypatch.setattr(search, "hybrid_notes", fake_hybrid)
    architect._tool_reference_lookup(conn, "what did jeff hopkins say about asthma", 6)
    assert captured.get("entity_expand", False) is False, "reference_lookup must not enable expansion"


def test_search_notes_tool_enables_expansion(conn, monkeypatch):
    """Positive scope: _tool_search_notes and _tool_find DO pass entity_expand=True (the owner
    chat tools where span detection is meant to fire)."""
    from app.services import architect, search
    captured = []

    def fake_hybrid(c, q, limit=8, *, entity_expand=False):
        captured.append(entity_expand)
        return []

    monkeypatch.setattr(search, "hybrid_notes", fake_hybrid)
    architect._tool_search_notes(conn, "what did jeff hopkins say", 8)
    architect._tool_find(conn, "what did jeff hopkins say", 6)
    assert captured == [True, True], "search_notes and find must enable expansion"
