"""Owner nickname aliases self-heal on every entity_index.rebuild.

An owner who set their name BEFORE the alias feature shipped never re-saved Owner settings,
so their name/declared-aliases were never seeded as durable entity_decisions('alias'). rebuild()
now re-runs wiki_build.reconcile_owner (idempotent + split-gated, best-effort) so those decisions
get backfilled. The seeded decisions fold into entity_aliases on the FOLLOWING rebuild (loaded by
decided_aliases at the start of the next pass) — expected eventual-consistency.

Embeddings + the entity-embedding sync are monkeypatched so the suite runs without the model.
"""
import json
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
    # Entity-embedding sync pulls fastembed; no-op it so rebuild() runs offline.
    monkeypatch.setattr(entity_index, "_sync_embeddings", lambda *a, **k: None)

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    c = db.get_conn()
    db.ensure_default_person(c)      # the 'Me' owner row link_owner/reconcile_owner read
    return c


# ---- helpers ----------------------------------------------------------------------------

def _mk(conn, title, body="x", kind="kb"):
    from app.services import notes as notes_svc
    notes_svc.upsert_note(conn, title, body, kind=kind, fire_events=False)
    conn.commit()
    return notes_svc.get_by_title(conn, title)


def _analyzed_note(conn, title, ent_name, etype="person"):
    """A live 'entry' note whose analysis names one entity — drives entity_index.rebuild."""
    note = _mk(conn, title, f"about {ent_name}", kind="entry")
    conn.execute(
        "INSERT INTO note_analysis (note_id, content_hash, entities_json) VALUES (?,?,?)",
        (note["id"], "h", json.dumps([{"name": ent_name, "type": etype}])))
    conn.commit()
    return note


def _set_owner(conn, name, aliases=""):
    conn.execute("UPDATE people SET name=?, aliases=? WHERE is_default=1", (name, aliases))
    conn.commit()


def _owner_aliases(conn):
    """The norm_a's recorded as aliases of the 'jeffrey hopkins' canonical."""
    from app.services import entity_decisions
    return [an for an, _ in entity_decisions.load_aliases(conn).get("jeffrey hopkins", [])]


# ---- backfill on rebuild ----------------------------------------------------------------

def test_rebuild_backfills_owner_alias_decision(conn):
    """An owner who set their name pre-feature gets a durable alias decision on rebuild,
    then it materializes into the alias surface on the FOLLOWING rebuild."""
    from app.services import entity_index, entity_decisions
    _analyzed_note(conn, "notes/2026/01/01", "Jeffrey Hopkins")
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _set_owner(conn, "Jeff Hopkins")

    entity_index.rebuild(conn)
    aliases = entity_decisions.load_aliases(conn)
    assert any(an == "jeff hopkins" for an, _ in aliases.get("jeffrey hopkins", [])), \
        "rebuild should have seeded the owner's nickname as an alias decision"

    # One-rebuild lag: the seeded decision folds into the alias surface on the next pass.
    entity_index.rebuild(conn)
    surf = entity_index.alias_surface(conn)
    assert surf.get("jeff hopkins") == ("kb/People/Jeffrey Hopkins", "Jeff Hopkins")


def test_unset_owner_seeds_no_alias(conn):
    """The default 'Me' owner is a placeholder → rebuild seeds no owner alias decisions."""
    from app.services import entity_index, entity_decisions
    _analyzed_note(conn, "notes/2026/01/02", "Jeffrey Hopkins")
    _mk(conn, "kb/People/Jeffrey Hopkins")
    # owner left at the default ('Me')

    entity_index.rebuild(conn)
    entity_index.rebuild(conn)
    assert entity_decisions.load_aliases(conn) == {}, \
        "an unset/placeholder owner must not seed any alias decisions"


def test_backfill_is_idempotent(conn):
    """Rebuilding repeatedly yields exactly one 'jeff hopkins' alias decision (no dupes)."""
    from app.services import entity_index
    _analyzed_note(conn, "notes/2026/01/03", "Jeffrey Hopkins")
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _set_owner(conn, "Jeff Hopkins")

    entity_index.rebuild(conn)
    entity_index.rebuild(conn)
    entity_index.rebuild(conn)
    assert _owner_aliases(conn).count("jeff hopkins") == 1


def test_backfill_split_gated(conn):
    """A user split ('Jeff Hopkins' is NOT 'Jeffrey Hopkins') must block the backfill fold."""
    from app.services import entity_index, entity_decisions
    _analyzed_note(conn, "notes/2026/01/04", "Jeffrey Hopkins")
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _set_owner(conn, "Jeff Hopkins")
    entity_decisions.add(conn, kind="split", norm_a="Jeff Hopkins", norm_b="Jeffrey Hopkins")
    conn.commit()

    entity_index.rebuild(conn)
    entity_index.rebuild(conn)
    assert "jeff hopkins" not in entity_index.alias_surface(conn)   # split wins over the fold
