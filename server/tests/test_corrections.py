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
