"""Source-of-truth correction promotion: a 'correction' talk item on a kb article is
promoted to a dated entry note (the truth layer) and linked back via article_talk.

Embedding calls are monkeypatched so the suite runs without the local model.
"""
import os
import re
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
    monkeypatch.setattr(embeddings, "upsert_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "delete_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()

    from app import auth
    auth.ensure_access_key()

    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})


def _make_article(client, title="kb/People/Bjorn", body="# Bjorn\n\nA person named Bjorn."):
    """Create a kb article (the kb/ prefix makes upsert_note set kind='kb'). No LLM."""
    r = client.post("/api/notes", json={"title": title, "content_md": body}).json()
    assert r["title"] == title
    return r["slug"]


def test_correction_promotes_to_dated_truth_note(client):
    slug = _make_article(client)
    r = client.post(f"/api/notes/{slug}/talk",
                    json={"kind": "correction", "body": "the name is spelled Bjørn, not Bjorn"})
    data = r.json()
    assert data["id"] is not None
    promoted = data["promoted"]
    assert promoted is not None, "a correction must promote to a note"
    # Lands in the standard flat dated capture tree as a normal entry.
    assert re.match(r"^notes/\d{4}/\d{2}/\d{2}/\d{2}$", promoted["title"]), promoted["title"]
    note = client.get(f"/api/notes/{promoted['slug']}").json()
    assert note["kind"] == "entry"
    assert note["content_md"].startswith("CORRECTION (source of truth): the name is spelled Bjørn")
    assert "[[kb/People/Bjorn]]" in note["content_md"]

    # The talk item is flagged and links to the truth note.
    talk = client.get(f"/api/notes/{slug}/talk").json()
    corr = [t for t in talk if t["kind"] == "correction"]
    assert len(corr) == 1
    assert corr[0]["is_correction"] == 1
    assert corr[0]["source_note_slug"] == promoted["slug"]


def test_non_correction_kinds_do_not_promote(client):
    slug = _make_article(client)
    for kind in ("directive", "note", "question", "todo"):
        r = client.post(f"/api/notes/{slug}/talk",
                        json={"kind": kind, "body": f"a {kind} that is not a fact fix"}).json()
        assert r["promoted"] is None, f"{kind} must not promote"
    talk = client.get(f"/api/notes/{slug}/talk").json()
    assert all(t.get("is_correction") in (0, None) for t in talk)
    # No dated truth notes were created.
    notes = client.get("/api/notes", params={"q": "notes/"}).json()
    assert not any(n["title"].startswith("notes/") and "/" in n["title"][6:] for n in notes)


def test_duplicate_correction_does_not_spawn_second_note(client):
    slug = _make_article(client)
    first = client.post(f"/api/notes/{slug}/talk",
                        json={"kind": "correction", "body": "Born in 1990 not 1989"}).json()
    assert first["promoted"] is not None
    # Identical after normalization (case + whitespace + trailing punctuation) → deduped.
    second = client.post(f"/api/notes/{slug}/talk",
                         json={"kind": "correction", "body": "born in 1990   not 1989."}).json()
    assert second["promoted"] is None

    talk = client.get(f"/api/notes/{slug}/talk").json()
    corr = [t for t in talk if t["kind"] == "correction"]
    assert len(corr) == 1, "the redundant re-submit should have been dropped"


def test_deleting_truth_note_keeps_talk_record_but_nulls_link(client):
    slug = _make_article(client)
    promoted = client.post(f"/api/notes/{slug}/talk",
                           json={"kind": "correction", "body": "City is Oslo, not Olso."}).json()["promoted"]
    client.delete(f"/api/notes/{promoted['slug']}")
    talk = client.get(f"/api/notes/{slug}/talk").json()
    corr = [t for t in talk if t["kind"] == "correction"]
    assert len(corr) == 1
    assert corr[0]["is_correction"] == 1            # the record survives
    assert corr[0]["source_note_slug"] is None       # link drops (ON DELETE SET NULL)


def test_rename_carries_corrections_to_new_title(client, monkeypatch):
    slug = _make_article(client, title="kb/People/Bjorn")
    client.post(f"/api/notes/{slug}/talk",
                json={"kind": "correction", "body": "the name is spelled Bjørn"})

    # Isolate the talk rekey from the entity/index machinery.
    from app.services import wiki_build, entity_index
    monkeypatch.setattr(entity_index, "rebuild", lambda *a, **k: 0)
    monkeypatch.setattr(wiki_build, "flag_dead_links", lambda *a, **k: None)
    monkeypatch.setattr(wiki_build, "refresh_index", lambda *a, **k: None)

    import app.db as db
    conn = db.get_conn()
    out = wiki_build.recategorize_article(conn, "kb/People/Bjorn", "kb/People/Norway/Bjorn")
    assert out["ok"], out

    from app.services import article_talk
    assert article_talk.open_for(conn, "kb/People/Bjorn") == []
    moved = article_talk.open_for(conn, "kb/People/Norway/Bjorn")
    assert len(moved) == 1 and moved[0]["kind"] == "correction"
    # The entity override (recorded because the body is a name fix) follows the rename too.
    assert conn.execute("SELECT 1 FROM entity_overrides WHERE article_title=?",
                        ("kb/People/Norway/Bjorn",)).fetchone() is not None
    assert conn.execute("SELECT 1 FROM entity_overrides WHERE article_title=?",
                        ("kb/People/Bjorn",)).fetchone() is None


# ---- Phase 2: durable entity-name healing ----

def test_extract_corrected_name():
    from app.services.corrections import extract_corrected_name
    assert extract_corrected_name("the name is spelled Bjørn, not Bjorn") == "Bjørn"
    assert extract_corrected_name("her name is Mary Jane Watson") == "Mary Jane Watson"
    assert extract_corrected_name("also known as Benny.") == "Benny"
    # Not a name fix → no extraction (so a non-name correction never mis-sets an entity name).
    assert extract_corrected_name("the year is 1990, not 1989") is None
    assert extract_corrected_name("expand the early-life section") is None


def _insert_entity(conn, *, typ="person", key="bjorn", name="Bjorn", article="kb/People/Bjorn"):
    conn.execute(
        "INSERT INTO entities (type, canonical_name, normalized_key, note_count, article_title) "
        "VALUES (?,?,?,1,?)", (typ, name, key, article))
    conn.commit()
    return conn.execute("SELECT id FROM entities WHERE type=? AND normalized_key=?",
                        (typ, key)).fetchone()["id"]


def test_set_canonical_override_patches_entity_and_aliases(client):
    import app.db as db
    from app.services import entity_index
    conn = db.get_conn()
    eid = _insert_entity(conn)
    entity_index.set_canonical_override(conn, "kb/People/Bjorn", "Bjørn", source_note_id=None)
    conn.commit()
    row = conn.execute("SELECT canonical_name FROM entities WHERE id=?", (eid,)).fetchone()
    assert row["canonical_name"] == "Bjørn"
    aliases = {a["alias_norm"] for a in conn.execute(
        "SELECT alias_norm FROM entity_aliases WHERE entity_id=?", (eid,)).fetchall()}
    assert "bjorn" in aliases                       # the old spelling still resolves
    assert entity_index.normalize("Bjørn") in aliases   # and the corrected one


def test_apply_overrides_skips_deleted_source_note(client):
    import app.db as db
    from app.services import entity_index, notes as notes_svc
    conn = db.get_conn()
    eid = _insert_entity(conn)
    # A correction note backs the override.
    nid = notes_svc.upsert_note(conn, notes_svc.next_dated_title(conn, __import__("datetime").date.today()),
                                "CORRECTION (source of truth): spelled Bjørn", source="user", kind="entry")
    entity_index.set_canonical_override(conn, "kb/People/Bjorn", "Bjørn", source_note_id=nid)
    conn.commit()
    # Simulate a rebuild recomputing the frequency name, then re-applying overrides.
    conn.execute("UPDATE entities SET canonical_name='Bjorn' WHERE id=?", (eid,))
    entity_index._apply_overrides(conn)
    assert conn.execute("SELECT canonical_name FROM entities WHERE id=?", (eid,)).fetchone()["canonical_name"] == "Bjørn"
    # Delete the source note → the override is skipped → the name auto-reverts on rebuild.
    notes_svc.soft_delete(conn, nid)
    conn.execute("UPDATE entities SET canonical_name='Bjorn' WHERE id=?", (eid,))
    entity_index._apply_overrides(conn)
    assert conn.execute("SELECT canonical_name FROM entities WHERE id=?", (eid,)).fetchone()["canonical_name"] == "Bjorn"


def test_promotion_records_entity_override(client):
    import app.db as db
    from app.services import entity_index
    conn = db.get_conn()
    slug = _make_article(client, title="kb/People/Bjorn")
    _insert_entity(conn)
    client.post(f"/api/notes/{slug}/talk",
                json={"kind": "correction", "body": "the name is spelled Bjørn, not Bjorn"})
    conn = db.get_conn()
    ov = conn.execute("SELECT canonical_name, source_note_id FROM entity_overrides "
                      "WHERE article_title='kb/People/Bjorn'").fetchone()
    assert ov is not None and ov["canonical_name"] == "Bjørn"
    assert ov["source_note_id"] is not None         # links to the promoted truth note
    # Immediate effect: the linked entity already displays the corrected name.
    name = conn.execute("SELECT canonical_name FROM entities WHERE article_title='kb/People/Bjorn'").fetchone()
    assert name["canonical_name"] == "Bjørn"
