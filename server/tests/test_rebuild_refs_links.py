"""Rebuild engine hardening: citation-title repair, the in-memory add-link backstop,
source-mention seeding of the cross-link vocabulary, and the References lint.

Embedding calls are monkeypatched (same as test_redirects/test_api) so the suite runs
without the model.
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

    from app.services import embeddings
    for name in ("upsert_note_embedding", "delete_note_embedding",
                 "upsert_attachment_embeddings", "delete_attachment_embeddings"):
        monkeypatch.setattr(embeddings, name, lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "semantic_search_attachments", lambda *a, **k: [])

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    return db.get_conn()


def _mk(conn, title, body="x", kind="kb"):
    from app.services import notes as notes_svc
    notes_svc.upsert_note(conn, title, body, kind=kind, fire_events=False)
    conn.commit()
    return notes_svc.get_by_title(conn, title)


# --- _repair_citation_titles (pure) ----------------------------------------------------

def test_repair_corrects_a_mistyped_source_title():
    from app.services.wiki_build import _repair_citation_titles
    # The writer cited a normalised near-miss of the real curated source title.
    content = "The code is 82544.[^s1]\n\n## References\n\n[^s1]: [[Ford Keyless Entry Code 82544]] — 2026-06-01\n"
    sources = ["Ford  Keyless  Entry  Code  82544"]   # extra spaces — same normalised key
    out, still_bad = _repair_citation_titles(content, ["Ford Keyless Entry Code 82544"], sources)
    assert "[[Ford  Keyless  Entry  Code  82544]]" in out
    assert still_bad == []


def test_repair_refuses_when_two_sources_match():
    from app.services.wiki_build import _repair_citation_titles
    # Two distinct curated titles normalise-equal → ambiguous → leave it 'bad' (don't guess).
    out, still_bad = _repair_citation_titles(
        "[[trip 2025]]", ["trip 2025"], ["Trip 2025", "TRIP  2025"])
    assert still_bad == ["trip 2025"]
    assert out == "[[trip 2025]]"            # unchanged


def test_repair_preserves_a_display_alias():
    from app.services.wiki_build import _repair_citation_titles
    out, still_bad = _repair_citation_titles(
        "see [[Ford  Note|the note]]", ["Ford  Note"], ["Ford Note"])
    assert out == "see [[Ford Note|the note]]"   # title fixed, display alias preserved
    assert still_bad == []


# --- add_links_to_content (DB) ---------------------------------------------------------

def test_add_links_links_a_bare_person_mention(conn):
    from app.services.wiki_build import add_links_to_content
    _mk(conn, "kb/People/Jeff Hopkins")
    body = "# Ford Truck\n\nJeff Hopkins owns this truck.\n"
    new, props = add_links_to_content(conn, "kb/Things/Ford Truck", body)
    assert "[[kb/People/Jeff Hopkins|Jeff Hopkins]]" in new
    assert any(p["target"] == "kb/People/Jeff Hopkins" for p in props)


def test_add_links_skips_reference_target(conn):
    from app.services.wiki_build import add_links_to_content
    _mk(conn, "kb/People/Jeff Hopkins")
    body = "# Asthma\n\nJeff Hopkins is unrelated; this stays general.\n"
    new, props = add_links_to_content(conn, "kb/Reference/Medicine/Conditions/Asthma", body)
    assert props == []
    assert new == body                       # PII firewall: never name a person in Reference


def test_add_links_does_not_link_inside_a_citation(conn):
    from app.services.wiki_build import add_links_to_content
    _mk(conn, "kb/People/Jeff Hopkins")
    body = "# Truck\n\nIt is owned.[^s1]\n\n## References\n\n[^s1]: [[Jeff Hopkins note]] — 2026-01-01\n"
    new, _ = add_links_to_content(conn, "kb/Things/Truck", body)
    # The footnote-definition line is masked, so the existing [[...]] is untouched.
    assert new.count("[[") == body.count("[[")


# --- scoped_known_titles source-mention seeding (DB) ----------------------------------

def test_scoped_known_titles_seeds_mentioned_entity_articles(conn):
    from app.services import wiki_build
    note = _mk(conn, "notes/2026/01/source", "Jeff did things", kind="entry")
    art = _mk(conn, "kb/People/Jeff Hopkins")
    # Register the entity + its mention in the source note, pointing at the People article.
    conn.execute("INSERT INTO entities (type, canonical_name, normalized_key, article_title) "
                 "VALUES ('person','Jeff Hopkins','jeff hopkins',?)", (art["title"],))
    eid = conn.execute("SELECT id FROM entities WHERE article_title=?", (art["title"],)).fetchone()["id"]
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (eid, note["id"]))
    conn.commit()
    # A large 'all_titles' forces the budgeted/scoped path; the mentioned People page must
    # survive even though it's in a different folder from the target.
    all_titles = [f"kb/Things/Filler {i}" for i in range(700)] + ["kb/People/Jeff Hopkins"]
    out = wiki_build.scoped_known_titles(conn, "kb/Things/Truck", all_titles,
                                         budget=600, source_ids=[note["id"]])
    assert "kb/People/Jeff Hopkins" in out


def test_scoped_known_titles_skips_seeding_for_reference_target(conn):
    from app.services import wiki_build
    note = _mk(conn, "notes/2026/01/source2", "Jeff did things", kind="entry")
    art = _mk(conn, "kb/People/Jeff Hopkins")
    conn.execute("INSERT INTO entities (type, canonical_name, normalized_key, article_title) "
                 "VALUES ('person','Jeff Hopkins','jeff hopkins',?)", (art["title"],))
    eid = conn.execute("SELECT id FROM entities WHERE article_title=?", (art["title"],)).fetchone()["id"]
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (eid, note["id"]))
    conn.commit()
    all_titles = [f"kb/Reference/Medicine/Conditions/C{i}" for i in range(700)] + ["kb/People/Jeff Hopkins"]
    out = wiki_build.scoped_known_titles(conn, "kb/Reference/Medicine/Conditions/Asthma",
                                         all_titles, budget=600, source_ids=[note["id"]])
    # The People page is NOT pulled in for a Reference target (it may still appear via the
    # alphabetical top-up only if it sorts in — but it must not be SEEDED). With 700 Reference
    # fillers sorting before 'kb/People', the top-up won't reach it.
    assert "kb/People/Jeff Hopkins" not in out
