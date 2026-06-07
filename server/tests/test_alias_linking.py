"""Alias-aware KB linking + identity reconciliation.

The deterministic linker links a registered alias surface ("Jeff Hopkins") to its canonical
article ("kb/People/Jeffrey Hopkins") with a piped display, the label-hygiene sweep is taught
to leave such links alone, and the owner/alias reconciliation makes a nickname a durable alias.

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


# ---- A. alias_surface drop-rules --------------------------------------------------------

def test_alias_surface_includes_public_multiword_alias(conn):
    from app.services import entity_index
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    surf = entity_index.alias_surface(conn)
    assert surf.get("jeff hopkins") == ("kb/People/Jeffrey Hopkins", "Jeff Hopkins")


def test_alias_surface_drops_ambiguous_alias(conn):
    from app.services import entity_index
    _mk(conn, "kb/People/Jeffrey Hopkins"); _mk(conn, "kb/People/Jeff Hopkinson")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["JH"])
    _entity(conn, "Jeff Hopkinson", "kb/People/Jeff Hopkinson", aliases=["JH"])
    surf = entity_index.alias_surface(conn)
    assert "jh" not in surf            # alias maps to two article-bearing entities → dropped


def test_alias_surface_drops_single_token_without_decision(conn):
    from app.services import entity_index
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff"])
    assert "jeff" not in entity_index.alias_surface(conn)


def test_alias_surface_includes_single_token_with_decision(conn):
    from app.services import entity_index, entity_decisions
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff"])
    entity_decisions.add(conn, kind="alias", norm_a="Jeff", norm_b="Jeffrey Hopkins", display_a="Jeff")
    conn.commit()
    assert entity_index.alias_surface(conn).get("jeff") == ("kb/People/Jeffrey Hopkins", "Jeff")


def test_alias_surface_drops_shadowing_leaf(conn):
    from app.services import entity_index
    # An alias whose norm equals a real article leaf is left to the leaf path.
    _mk(conn, "kb/People/Jeffrey Hopkins"); _mk(conn, "kb/People/Bob Jones")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Bob Jones"])
    assert "bob jones" not in entity_index.alias_surface(conn)


def test_alias_surface_drops_private_and_reference_targets(conn):
    from app.services import entity_index
    _mk(conn, "kb/Health/Jeffrey Hopkins"); _mk(conn, "kb/Reference/Medicine/Conditions/Asthma")
    _entity(conn, "Jeffrey Hopkins", "kb/Health/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    _entity(conn, "Asthma", "kb/Reference/Medicine/Conditions/Asthma", etype="condition", aliases=["Wheeze Disease"])
    surf = entity_index.alias_surface(conn)
    assert "jeff hopkins" not in surf and "wheeze disease" not in surf


# ---- B. linker alias pass ---------------------------------------------------------------

def test_links_alias_surface_to_canonical(conn):
    from app.services import wiki_build
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    new, props = wiki_build.add_links_to_content(conn, "kb/Things/Ford Truck", "Jeff Hopkins owns this truck.")
    assert "[[kb/People/Jeffrey Hopkins|Jeff Hopkins]]" in new
    assert any(p["target"] == "kb/People/Jeffrey Hopkins" for p in props)


def test_alias_not_linked_into_reference_article(conn):
    from app.services import wiki_build
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    body = "# Asthma\n\nJeff Hopkins is unrelated.\n"
    new, props = wiki_build.add_links_to_content(conn, "kb/Reference/Medicine/Conditions/Asthma", body)
    assert new == body and props == []


def test_alias_not_linked_inside_citation(conn):
    from app.services import wiki_build
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    body = "Owned.[^s1]\n\n## References\n\n[^s1]: [[Jeff Hopkins diary]] — 2026-01-01\n"
    new, _ = wiki_build.add_links_to_content(conn, "kb/Things/Truck", body)
    assert new.count("[[") == body.count("[[")     # footnote line is masked → no new link


def test_alias_skipped_when_canonical_already_linked(conn):
    from app.services import wiki_build
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    body = "See [[kb/People/Jeffrey Hopkins]]. Also Jeff Hopkins again."
    new, _ = wiki_build.add_links_to_content(conn, "kb/Things/Truck", body)
    assert "[[kb/People/Jeffrey Hopkins|Jeff Hopkins]]" not in new   # one link per target


# ---- C. hygiene allow-list --------------------------------------------------------------

def test_registered_alias_link_not_flagged(conn):
    from app.services import wikilinks
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    _mk(conn, "kb/Things/Truck", "Owned by [[kb/People/Jeffrey Hopkins|Jeff Hopkins]].")
    findings = wikilinks.scan_link_labels(conn)
    assert not any(f["raw"] == "[[kb/People/Jeffrey Hopkins|Jeff Hopkins]]" for f in findings)


def test_verbose_label_still_flagged(conn):
    from app.services import wikilinks
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    # Display echoes the article's own name → not a registered alias → still tidied.
    _mk(conn, "kb/Things/Truck", "Owned by [[kb/People/Jeffrey Hopkins|Jeffrey Hopkins]].")
    findings = wikilinks.scan_link_labels(conn)
    assert any(f["target"] == "kb/People/Jeffrey Hopkins" for f in findings)


def test_hygiene_fails_closed_when_surface_errors(conn, monkeypatch):
    from app.services import wikilinks, entity_index
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _mk(conn, "kb/Things/Truck", "Owned by [[kb/People/Jeffrey Hopkins|Jeffrey Hopkins]].")
    monkeypatch.setattr(entity_index, "alias_surface", lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    assert wikilinks.scan_link_labels(conn) == []     # strip nothing on an indeterminate surface


def test_no_oscillation_normalize_leaves_alias_link(conn):
    from app.services import wikilinks
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    n = _mk(conn, "kb/Things/Truck", "Owned by [[kb/People/Jeffrey Hopkins|Jeff Hopkins]].")
    res = wikilinks.normalize_all_link_labels(conn)
    from app.services import notes as notes_svc
    body = notes_svc.get_by_title(conn, "kb/Things/Truck")["content_md"]
    assert "[[kb/People/Jeffrey Hopkins|Jeff Hopkins]]" in body and res["fixed"] == 0


# ---- D. rebuild split-gate --------------------------------------------------------------

def test_alias_decision_folds_into_entity_aliases(conn):
    from app.services import entity_index, entity_decisions
    _analyzed_note(conn, "notes/2026/01/01", "Jeffrey Hopkins")
    _mk(conn, "kb/People/Jeffrey Hopkins")
    entity_index.rebuild(conn)
    entity_decisions.add(conn, kind="alias", norm_a="Jeff Hopkins", norm_b="Jeffrey Hopkins", display_a="Jeff Hopkins")
    conn.commit()
    entity_index.rebuild(conn)
    assert "jeff hopkins" in entity_index.alias_surface(conn)


def test_split_gate_blocks_alias_fold(conn):
    from app.services import entity_index, entity_decisions
    _analyzed_note(conn, "notes/2026/01/02", "Jeffrey Hopkins")
    _mk(conn, "kb/People/Jeffrey Hopkins")
    entity_index.rebuild(conn)
    entity_decisions.add(conn, kind="alias", norm_a="Jeff Hopkins", norm_b="Jeffrey Hopkins", display_a="Jeff Hopkins")
    entity_decisions.add(conn, kind="split", norm_a="Jeff Hopkins", norm_b="Jeffrey Hopkins")
    conn.commit()
    entity_index.rebuild(conn)
    assert "jeff hopkins" not in entity_index.alias_surface(conn)   # split wins over the alias row


# ---- E. link_owner / reconcile_owner ----------------------------------------------------

def test_link_owner_binds_via_nickname_exactly_one(conn):
    from app.services import wiki_build, people
    art = _mk(conn, "kb/People/Jeffrey Hopkins")
    _set_owner(conn, "Jeff Hopkins")
    res = wiki_build.link_owner(conn)
    assert res["linked"] == "kb/People/Jeffrey Hopkins"
    assert people.owner(conn)["note_slug"] == art["slug"]


def test_link_owner_refuses_two_same_surname(conn):
    from app.services import wiki_build, people
    _mk(conn, "kb/People/Jeffery Hopkins"); _mk(conn, "kb/People/Geoffrey Hopkins")
    _set_owner(conn, "Jeff Hopkins")          # both pages nickname→"jeffrey hopkins" → ambiguous
    assert wiki_build.link_owner(conn)["linked"] is None
    assert people.owner(conn)["note_slug"] is None


def test_reconcile_owner_seeds_alias_and_links_after_rebuild(conn):
    from app.services import wiki_build, entity_index
    _analyzed_note(conn, "notes/2026/01/03", "Jeffrey Hopkins")
    _mk(conn, "kb/People/Jeffrey Hopkins")
    entity_index.rebuild(conn)
    _set_owner(conn, "Jeff Hopkins")
    res = wiki_build.reconcile_owner(conn)
    assert res["linked"] == "kb/People/Jeffrey Hopkins" and res["aliases_seeded"] >= 1
    entity_index.rebuild(conn)                # materialize the seeded alias into entity_aliases
    new, _ = wiki_build.add_links_to_content(conn, "kb/Things/Ford Truck", "Jeff Hopkins owns this.")
    assert "[[kb/People/Jeffrey Hopkins|Jeff Hopkins]]" in new


def test_reconcile_owner_seeds_before_any_rebuild(conn):
    # The young-KB gap: no entity bound yet. reconcile must still record the alias decision,
    # anchored on the article-leaf norm, so the next rebuild folds it.
    from app.services import wiki_build, entity_decisions
    _mk(conn, "kb/People/Jeffrey Hopkins")          # article exists; entity index NOT built
    _set_owner(conn, "Jeff Hopkins")
    res = wiki_build.reconcile_owner(conn)
    assert res["linked"] == "kb/People/Jeffrey Hopkins" and res["aliases_seeded"] >= 1
    aliases = entity_decisions.load_aliases(conn)
    assert any(an == "jeff hopkins" for an, _ in aliases.get("jeffrey hopkins", []))


def test_reconcile_owner_split_gated(conn):
    from app.services import wiki_build, entity_decisions
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _set_owner(conn, "Jeff Hopkins")
    entity_decisions.add(conn, kind="split", norm_a="Jeff Hopkins", norm_b="Jeffrey Hopkins")
    conn.commit()
    wiki_build.reconcile_owner(conn)
    aliases = entity_decisions.load_aliases(conn)
    assert not any(an == "jeff hopkins" for an, _ in aliases.get("jeffrey hopkins", []))


def test_link_owner_binds_via_declared_alias(conn):
    from app.services import wiki_build
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _set_owner(conn, "Boss", aliases="Jeffrey Hopkins")   # exact declared alias → strong match
    assert wiki_build.link_owner(conn)["linked"] == "kb/People/Jeffrey Hopkins"


def test_check_needed_links_alias_pass_auto(conn):
    from app.services import wiki_build, notes as notes_svc
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    _mk(conn, "kb/Things/Truck", "Jeff Hopkins owns this truck.")
    out = wiki_build.check_needed_links(conn, "kb/Things/Truck", mode="auto")
    body = notes_svc.get_by_title(conn, "kb/Things/Truck")["content_md"]
    assert "[[kb/People/Jeffrey Hopkins|Jeff Hopkins]]" in body
    assert any(p["target"] == "kb/People/Jeffrey Hopkins"
               for a in out["articles"] for p in a["proposals"])


def test_check_needed_links_alias_skips_reference_target(conn):
    from app.services import wiki_build
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    _mk(conn, "kb/Reference/Medicine/Conditions/Asthma", "Jeff Hopkins is unrelated.")
    out = wiki_build.check_needed_links(conn, "kb/Reference/Medicine/Conditions/Asthma", mode="auto")
    assert out["count"] == 0


def test_multi_alias_same_target_links_once(conn):
    from app.services import wiki_build, entity_decisions
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins", "Jeff M Hopkins"])
    new, _ = wiki_build.add_links_to_content(
        conn, "kb/Things/Truck", "Owned by Jeff Hopkins, also written Jeff M Hopkins.")
    assert new.count("[[kb/People/Jeffrey Hopkins|") == 1   # one link per canonical target


def test_linker_produced_alias_link_survives_hygiene(conn):
    # True end-to-end no-oscillation: the LINKER produces the piped link (mixed case/spacing),
    # then the hygiene sweep must leave it intact.
    from app.services import wiki_build, wikilinks, notes as notes_svc
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    new, _ = wiki_build.add_links_to_content(conn, "kb/Things/Truck", "Owned by JEFF  Hopkins today.")
    notes_svc.upsert_note(conn, "kb/Things/Truck", new, kind="kb", fire_events=False)
    conn.commit()
    res = wikilinks.normalize_all_link_labels(conn)
    body = notes_svc.get_by_title(conn, "kb/Things/Truck")["content_md"]
    assert "[[kb/People/Jeffrey Hopkins|JEFF  Hopkins]]" in body and res["fixed"] == 0


# ---- F. writer-prompt {known_aliases} offer ---------------------------------------------

def test_known_aliases_block_lists_offer(conn):
    from app.services import wiki_build
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    block = wiki_build.known_aliases_block(conn, ["kb/People/Jeffrey Hopkins"])
    assert '"Jeff Hopkins" → kb/People/Jeffrey Hopkins' in block


def test_known_aliases_block_empty_when_target_not_in_set(conn):
    from app.services import wiki_build
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    assert wiki_build.known_aliases_block(conn, ["kb/Things/Truck"]) == "(none)"
    assert wiki_build.known_aliases_block(conn, []) == "(none)"


def test_build_write_prompt_injects_alias_offer_and_no_leftover_placeholder(conn):
    from app.services import wiki_build
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    art = {"title": "kb/Things/Ford Truck", "domain": "Things", "scope": "a truck", "sources": []}
    prompt = wiki_build.build_write_prompt(conn, art, [], known_titles=["kb/People/Jeffrey Hopkins"])
    assert "{known_aliases}" not in prompt              # placeholder fully substituted
    assert '"Jeff Hopkins" → kb/People/Jeffrey Hopkins' in prompt


# ---- G. search entity-expansion (owner chat) --------------------------------------------

def test_hybrid_notes_entity_expand_reaches_alias_notes(conn):
    from app.services import search
    n1 = _mk(conn, "notes/2026/01/10", "had lunch downtown today", kind="entry")  # body has no "Jeff"
    eid = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (eid, n1["id"]))
    conn.commit()
    base = search.hybrid_notes(conn, "Jeff Hopkins", 8)                       # alias-blind
    assert all(r["id"] != n1["id"] for r in base)
    exp = search.hybrid_notes(conn, "Jeff Hopkins", 8, entity_expand=True)    # entity channel
    assert any(r["id"] == n1["id"] for r in exp)


def test_hybrid_notes_entity_expand_ignores_sentence_query(conn):
    from app.services import search
    n1 = _mk(conn, "notes/2026/01/11", "had lunch downtown today", kind="entry")
    eid = _entity(conn, "Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (eid, n1["id"]))
    conn.commit()
    # The whole query isn't a bare name/alias → must NOT expand into the person's corpus.
    exp = search.hybrid_notes(conn, "what did jeff hopkins eat", 8, entity_expand=True)
    assert all(r["id"] != n1["id"] for r in exp)


def test_hybrid_notes_entity_expand_skips_ambiguous_name(conn):
    # "Jeff" maps to two distinct entities → ambiguous → must NOT expand either corpus.
    from app.services import search
    na = _mk(conn, "notes/2026/02/01", "lunch a", kind="entry")
    nb = _mk(conn, "notes/2026/02/02", "lunch b", kind="entry")
    ea = _entity(conn, "Jeff Hopkins", aliases=["Jeff"])
    eb = _entity(conn, "Jeff Stevens", aliases=["Jeff"])
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (ea, na["id"]))
    conn.execute("INSERT INTO entity_mentions (entity_id, note_id) VALUES (?,?)", (eb, nb["id"]))
    conn.commit()
    exp = search.hybrid_notes(conn, "Jeff", 8, entity_expand=True)
    assert all(r["id"] not in (na["id"], nb["id"]) for r in exp)   # ambiguous → no blend


def test_write_one_prompt_has_no_leftover_alias_placeholder(conn, monkeypatch):
    # write_one builds its own prompt; ensure {known_aliases} is substituted (offer present).
    from app.services import wiki_build, llm
    src = _mk(conn, "notes/2026/02/03", "Jeff Hopkins bought a Ford truck.", kind="entry")
    _mk(conn, "kb/People/Jeffrey Hopkins")
    _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins", aliases=["Jeff Hopkins"])
    seen = {}
    def fake_complete(messages, **kw):
        seen["prompt"] = messages[0]["content"]
        return "# Ford Truck\n\nA truck owned by [[kb/People/Jeffrey Hopkins|Jeff Hopkins]].\n", None
    monkeypatch.setattr(llm, "complete_with_meta", fake_complete)
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    art = {"title": "kb/Things/Ford Truck", "domain": "Things", "scope": "truck", "sources": [src["id"]]}
    wiki_build.write_one(conn, art, known_titles=["kb/People/Jeffrey Hopkins"])
    assert "{known_aliases}" not in seen["prompt"]
    assert '"Jeff Hopkins" → kb/People/Jeffrey Hopkins' in seen["prompt"]


def test_both_prompt_templates_carry_known_aliases_placeholder():
    from app.services import prompts
    assert "{known_aliases}" in prompts.get("actions.wiki_write", "")
    assert "{known_aliases}" in prompts.get("actions.wiki_maintain", "")


def test_notes_for_returns_normalized_key(conn):
    from app.services import entity_index
    _mk(conn, "kb/People/Jeffrey Hopkins")
    eid = _entity(conn, "Jeffrey Hopkins", "kb/People/Jeffrey Hopkins")
    detail = entity_index.notes_for(conn, eid)
    assert detail["normalized_key"] == "jeffrey hopkins"
