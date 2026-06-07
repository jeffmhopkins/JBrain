"""MAINTAIN path substitutes the {known_aliases} writer-prompt placeholder.

write_one's alias offer is already covered (test_alias_linking::test_write_one_prompt_*),
but maintain_one assembles its OWN prompt — the red-team review flagged that the MAINTAIN
side was untested. maintain_one only runs the LLM when there's something to do (open talk
items / new / removed sources), so we seed an OPEN 'directive' talk item, then monkeypatch
llm.complete to CAPTURE the prompt and return a minimal valid ```article + ```maintain
payload, and llm.has_credentials -> True.

Embeddings + the entity-embedding sync are monkeypatched so the suite runs without the
model (mirrors test_alias_linking.py).
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
    monkeypatch.setattr(entity_index, "_sync_embeddings", lambda *a, **k: None)

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    c = db.get_conn()
    db.ensure_default_person(c)
    return c


def _mk(conn, title, body="x", kind="kb"):
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


# A minimal but valid maintenance reply: a ```article fence + a ```maintain JSON block.
# _extract_maintain pulls the article body out of the ```article fence and the JSON out of
# ```maintain. The article keeps a non-stub structure so validate_structure doesn't loop.
_REPLY = (
    "```article\n"
    "# Ford Truck\n\n"
    "A truck owned by [[kb/People/Jeffrey Hopkins|Jeff Hopkins]].\n\n"
    "## References\n\n"
    "[^s1]: source\n"
    "```\n\n"
    "```maintain\n"
    '{"resolved": [], "new": []}\n'
    "```\n"
)


def test_maintain_one_prompt_substitutes_known_aliases(conn, monkeypatch):
    """maintain_one's assembled prompt must contain the alias offer line and NO leftover
    literal {known_aliases} placeholder."""
    from app.services import wiki_build, llm, article_talk

    # An article with an alias-bearing entity, plus an OPEN directive so maintain proceeds.
    art = _mk(conn, "kb/Things/Ford Truck", "# Ford Truck\n\nOwned by someone.\n", kind="kb")
    _mk(conn, "kb/People/Jeffrey Hopkins", "# Jeffrey Hopkins\nA person.", kind="kb")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    article_talk.add(conn, "kb/Things/Ford Truck", "directive", "Tighten the lead.")
    conn.commit()

    prompts_seen = []

    def fake_complete(messages, **kw):
        # maintain_one may make a SECOND (wiki_revise) call in its self-critique loop; the
        # FIRST call is the maintenance prompt that carries the {known_aliases} offer.
        prompts_seen.append(messages[0]["content"])
        return _REPLY

    # maintain_one's MAIN draft call goes through complete_with_meta -> (text, stop_reason);
    # the revise loop still uses complete. Patch both so the first captured prompt is the
    # maintenance prompt regardless of which path runs.
    monkeypatch.setattr(llm, "complete_with_meta", lambda messages, **kw: (fake_complete(messages, **kw), None))
    monkeypatch.setattr(llm, "complete", fake_complete)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)

    out = wiki_build.maintain_one(
        conn, "kb/Things/Ford Truck",
        known_titles=["kb/People/Jeffrey Hopkins", "kb/Things/Ford Truck"],
    )

    assert prompts_seen, "maintain_one should have called the LLM (open directive present)"
    maintain_prompt = prompts_seen[0]
    assert "{known_aliases}" not in maintain_prompt, "the {known_aliases} placeholder must be substituted"
    assert '"Jeff Hopkins" → kb/People/Jeffrey Hopkins' in maintain_prompt, "the alias offer line must be present"
    # The reply was consumed (sanity: the path actually ran maintenance, not the no-op branch).
    assert out["title"] == "kb/Things/Ford Truck"


def test_maintain_one_noop_when_nothing_to_do(conn, monkeypatch):
    """Guard for the test setup: with NO open items / new / removed sources, maintain_one
    short-circuits and never calls the LLM — confirming our directive is what drives it."""
    from app.services import wiki_build, llm

    _mk(conn, "kb/Things/Ford Truck", "# Ford Truck\n\nOwned by someone.\n", kind="kb")
    called = {"n": 0}

    def fake_complete(messages, **kw):
        called["n"] += 1
        return _REPLY

    monkeypatch.setattr(llm, "complete", fake_complete)
    monkeypatch.setattr(llm, "complete_with_meta", lambda messages, **kw: (fake_complete(messages, **kw), None))
    monkeypatch.setattr(llm, "has_credentials", lambda: True)

    out = wiki_build.maintain_one(conn, "kb/Things/Ford Truck", known_titles=["kb/Things/Ford Truck"])
    assert called["n"] == 0, "no open items → no LLM call"
    assert out["ok"] is True
