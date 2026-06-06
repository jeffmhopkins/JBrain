"""Phase 1 durable person-identity: merge/split/alias decisions that survive every
entity_index.rebuild() (which re-derives entities from note_analysis each pass).

Reuses the access-key + DB fixture style from test_api.py. The entity embedder is
monkeypatched so rebuild() never downloads/loads the local model.
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
    monkeypatch.setattr(embeddings, "upsert_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "delete_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "upsert_attachment_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "delete_attachment_embeddings", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "semantic_search_attachments", lambda *a, **k: [])
    # Entity rebuild embeds canonical entities — stub the model so tests stay offline/fast.
    monkeypatch.setattr(embeddings, "embed_many", lambda texts: [[0.0] for _ in texts])
    monkeypatch.setattr(embeddings, "store_entity_vector", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "delete_entity_embedding", lambda *a, **k: None)

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


def _mk(conn, title, ents):
    from app.services import notes as ns
    nid = ns.upsert_note(conn, title, "x")
    conn.execute("INSERT INTO note_analysis (note_id, content_hash, entities_json) VALUES (?,?,?)",
                 (nid, title, json.dumps(ents)))
    return nid


def _persons(conn):
    return {r["canonical_name"]: r["note_count"] for r in conn.execute(
        "SELECT canonical_name, note_count FROM entities WHERE type='person'").fetchall()}


def test_merge_unites_no_shared_token_clusters(client):
    """A user merge unites two clusters that share no distinctive token; the survivor keeps
    both notes and the other name becomes an alias. Idempotent across a second rebuild.

    Uses two names with NO nickname relationship (so the Phase-2 nickname heuristic doesn't
    pre-fold them) and no token-subset relationship — only the explicit merge unites them."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Orin Vasquez"}])
    _mk(conn, "n2", [{"type": "person", "name": "Mira Lindqvist"}])
    conn.commit()
    ei.rebuild(conn)
    # Heuristic alone: they DON'T fold (no shared distinctive token, no nickname relationship).
    assert len(_persons(conn)) == 2

    # Merge "Orin Vasquez" into "Mira Lindqvist".
    ed.add(conn, "merge", norm_a="Orin Vasquez", canonical="Mira Lindqvist")
    conn.commit()
    ei.rebuild(conn)
    persons = _persons(conn)
    assert list(persons) == ["Mira Lindqvist"]
    assert persons["Mira Lindqvist"] == 2          # both notes folded in
    # The merged-away name is an alias and resolves to the survivor's notes.
    survivor = conn.execute(
        "SELECT id FROM entities WHERE normalized_key='mira lindqvist'").fetchone()["id"]
    aliases = [a["alias_norm"] for a in conn.execute(
        "SELECT alias_norm FROM entity_aliases WHERE entity_id=?", (survivor,)).fetchall()]
    assert "orin vasquez" in aliases
    assert sorted(ei.note_ids_for_name(conn, "Orin Vasquez")) == \
        sorted(ei.note_ids_for_name(conn, "Mira Lindqvist"))

    # (2) Idempotency — rebuild AGAIN; still one merged entity (the core regression).
    ei.rebuild(conn)
    persons2 = _persons(conn)
    assert list(persons2) == ["Mira Lindqvist"] and persons2["Mira Lindqvist"] == 2


def test_split_blocks_heuristic_union(client):
    """A split forbids an auto-union the heuristic would otherwise make."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    _mk(conn, "s1", [{"type": "person", "name": "Sam J. Carter"}])
    _mk(conn, "s2", [{"type": "person", "name": "Sam Carter"}])
    conn.commit()
    ei.rebuild(conn)
    assert len(_persons(conn)) == 1                       # heuristic folds the subset name

    ed.add(conn, "split", norm_a="Sam Carter", norm_b="Sam J. Carter")
    conn.commit()
    ei.rebuild(conn)
    assert set(_persons(conn)) == {"Sam Carter", "Sam J. Carter"}   # split kept them apart
    # Idempotent.
    ei.rebuild(conn)
    assert set(_persons(conn)) == {"Sam Carter", "Sam J. Carter"}


def test_forced_canonical_is_the_chosen_name(client):
    """A merge's `canonical` wins even when it's the SHORTER (fewer-token) name, overriding
    the most-tokens heuristic."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    _mk(conn, "c1", [{"type": "person", "name": "Robert"}])
    _mk(conn, "c2", [{"type": "person", "name": "Robert Allan Downey"}])
    conn.commit()
    # Force the SHORTER name as canonical.
    ed.add(conn, "merge", norm_a="Robert Allan Downey", canonical="Robert")
    conn.commit()
    ei.rebuild(conn)
    persons = _persons(conn)
    assert list(persons) == ["Robert"] and persons["Robert"] == 2


def test_alias_decision_resolves(client):
    """An 'alias' decision lands in entity_aliases and is resolvable by name + index search."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    nid = _mk(conn, "a1", [{"type": "person", "name": "Margaret Hale"}])
    conn.commit()
    ei.rebuild(conn)
    eid = conn.execute("SELECT id FROM entities WHERE normalized_key='margaret hale'").fetchone()["id"]

    ed.add(conn, "alias", norm_a="Maggie", norm_b="margaret hale", display_a="Maggie")
    conn.commit()
    ei.rebuild(conn)
    aliases = {a["alias_norm"]: a["alias_display"] for a in conn.execute(
        "SELECT alias_norm, alias_display FROM entity_aliases WHERE entity_id=?", (eid,)).fetchall()}
    assert aliases.get("maggie") == "Maggie"
    assert ei.note_ids_for_name(conn, "Maggie") == [nid]
    assert any(e["canonical_name"] == "Margaret Hale" for e in ei.index(conn, q="Maggie"))


def test_mutual_exclusion_merge_clears_prior_split(client):
    """Recording a merge on a pair deletes any prior split on that same unordered pair."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    _mk(conn, "m1", [{"type": "person", "name": "Alex Stone"}])
    _mk(conn, "m2", [{"type": "person", "name": "Alexander Stone"}])
    conn.commit()

    ed.add(conn, "split", norm_a="Alex Stone", norm_b="Alexander Stone")
    conn.commit()
    ei.rebuild(conn)
    assert len(_persons(conn)) == 2                       # split holds them apart

    # A merge on the same pair must cancel the split, then unite them.
    ed.add(conn, "merge", norm_a="Alex Stone", canonical="Alexander Stone")
    conn.commit()
    splits = ed.load_splits(conn)
    assert frozenset({"alex stone", "alexander stone"}) not in splits
    ei.rebuild(conn)
    assert list(_persons(conn)) == ["Alexander Stone"]


def test_zero_mention_merge_is_dormant_noop(client):
    """A merge whose sides have NO clusters this pass materializes no entity and never errors."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    # No note_analysis rows at all — empty corpus.
    ed.add(conn, "merge", norm_a="Ghost One", canonical="Ghost Two")
    conn.commit()
    n = ei.rebuild(conn)                                  # must not raise
    assert n == 0 and _persons(conn) == {}
    # And a merge where only one side is absent: present side still produces ONE entity.
    _mk(conn, "g1", [{"type": "person", "name": "Real Person"}])
    conn.commit()
    ed.add(conn, "merge", norm_a="Real Person", canonical="Absent Person")
    conn.commit()
    ei.rebuild(conn)
    # 'Absent Person' has no cluster, so it's dormant: the present side keeps its own entity.
    persons = _persons(conn)
    assert persons == {"Real Person": 1}


def test_split_blocks_transitive_union(client):
    """Rep-based split check: a bare surname "Baker" would otherwise BRIDGE "Jon Baker" and
    "Jonathan Baker" into one group (each subsumes {baker}). A split between the two full
    names must survive that transitive path — they must NOT end up merged via the bridge."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    _mk(conn, "t1", [{"type": "person", "name": "Jon Baker"}])
    _mk(conn, "t2", [{"type": "person", "name": "Jonathan Baker"}])
    _mk(conn, "t3", [{"type": "person", "name": "Baker"}])
    conn.commit()
    ed.add(conn, "split", norm_a="Jon Baker", norm_b="Jonathan Baker")
    conn.commit()
    ei.rebuild(conn)
    # The split holds despite the "Baker" bridge: Jon Baker and Jonathan Baker stay distinct.
    assert set(_persons(conn)) == {"Jon Baker", "Jonathan Baker"}
    jon = ei.note_ids_for_name(conn, "Jon Baker")
    jonathan = ei.note_ids_for_name(conn, "Jonathan Baker")
    assert set(jon) != set(jonathan)            # not collapsed into one entity


def test_split_holds_with_bridge_variant_regardless_of_order(client):
    """Hard-invariant split: a bare surname "Lee" subsumes into Ann/Annie/Anna Lee and would
    bridge them all into one group. A split(Ann Lee, Anna Lee) must hold no matter which
    bridge union is processed first — the two split names never share an entity."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    a = _mk(conn, "L1", [{"type": "person", "name": "Ann Lee"}])
    _mk(conn, "L2", [{"type": "person", "name": "Annie Lee"}])
    b = _mk(conn, "L3", [{"type": "person", "name": "Anna Lee"}])
    _mk(conn, "L4", [{"type": "person", "name": "Lee"}])
    conn.commit()
    ed.add(conn, "split", norm_a="Ann Lee", norm_b="Anna Lee")
    conn.commit()
    ei.rebuild(conn)
    ann = set(ei.note_ids_for_name(conn, "Ann Lee"))
    anna = set(ei.note_ids_for_name(conn, "Anna Lee"))
    assert a in ann and b in anna
    assert ann.isdisjoint(anna)                 # the split survives the "Lee" bridge


def test_forced_merge_cannot_override_a_split(client):
    """Two merges routed to a common canonical must NOT co-group a split pair — the split is
    a hard invariant that wins over a (transitively) contradicting forced merge."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    c1 = _mk(conn, "v1", [{"type": "person", "name": "Chris Vee"}])
    c2 = _mk(conn, "v2", [{"type": "person", "name": "Christopher Vee"}])
    _mk(conn, "v3", [{"type": "person", "name": "Bridge Person"}])
    conn.commit()
    ed.add(conn, "split", norm_a="Chris Vee", norm_b="Christopher Vee")
    ed.add(conn, "merge", norm_a="Chris Vee", canonical="Bridge Person")
    ed.add(conn, "merge", norm_a="Christopher Vee", canonical="Bridge Person")
    conn.commit()
    ei.rebuild(conn)
    chris = set(ei.note_ids_for_name(conn, "Chris Vee"))
    christopher = set(ei.note_ids_for_name(conn, "Christopher Vee"))
    assert c1 in chris and c2 in christopher
    assert chris.isdisjoint(christopher)        # split wins over the bridging merges
    # The split row is NOT silently dropped by a transitive merge (only a same-pair merge cancels it).
    assert frozenset({"chris vee", "christopher vee"}) in ed.load_splits(conn)


def test_chained_merges_use_terminal_canonical(client):
    """merge(X->Y) + merge(Y->Z) collapses X, Y, Z into ONE entity whose canonical is the
    terminal target Z (the user's last-chosen name), not the most-tokens heuristic winner."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    _mk(conn, "x", [{"type": "person", "name": "Xavier Quinn Adams"}])    # most tokens
    _mk(conn, "y", [{"type": "person", "name": "Xq Adams"}])
    _mk(conn, "z", [{"type": "person", "name": "Quinn"}])                 # terminal, fewest tokens
    conn.commit()
    ed.add(conn, "merge", norm_a="Xavier Quinn Adams", canonical="Xq Adams")
    ed.add(conn, "merge", norm_a="Xq Adams", canonical="Quinn")
    conn.commit()
    ei.rebuild(conn)
    persons = _persons(conn)
    assert list(persons) == ["Quinn"] and persons["Quinn"] == 3


def test_split_alias_decisions_endpoints_round_trip(client):
    """HTTP round-trips for split, alias add/delete, and the decisions list."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "h1", [{"type": "person", "name": "Sam Carter"}])
    _mk(conn, "h2", [{"type": "person", "name": "Sam J. Carter"}])
    conn.commit()
    ei.rebuild(conn)
    assert len(_persons(conn)) == 1                         # heuristic folded them

    eid = conn.execute("SELECT id FROM entities WHERE type='person'").fetchone()["id"]
    # We need two ids to split; split the single entity is impossible, so split via decision
    # path requires both present — re-add a split through the API after a merge isn't sensible
    # here, so exercise alias + decisions on the single entity, and split on a 2-entity setup.
    r = client.post(f"/api/entities/{eid}/aliases", json={"display": "Sammy"})
    assert r.status_code == 200, r.text
    assert any(a == "sammy" for a in (
        x["alias_norm"] for x in conn.execute(
            "SELECT alias_norm FROM entity_aliases WHERE entity_id=?", (eid,)).fetchall()))

    d = client.get(f"/api/entities/{eid}/decisions")
    assert d.status_code == 200 and any(x["kind"] == "alias" for x in d.json())

    # Delete the alias back out.
    r = client.delete(f"/api/entities/{eid}/aliases/Sammy")
    assert r.status_code == 200, r.text
    assert "sammy" not in {x["alias_norm"] for x in conn.execute(
        "SELECT alias_norm FROM entity_aliases WHERE entity_id=?", (eid,)).fetchall()}


def test_split_endpoint_separates_two_entities(client):
    """The /split endpoint takes two entity ids and durably keeps them apart."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "p1", [{"type": "person", "name": "Sam Carter"}])
    _mk(conn, "p2", [{"type": "person", "name": "Sam J. Carter"}])
    conn.commit()
    ei.rebuild(conn)
    # Heuristic folded them into one; force a split needs two ids. Seed a third unrelated to
    # get a second entity, then split the folded survivor from it is not the case we want.
    # Instead: add a merge-then-split is overkill — directly assert the endpoint records a
    # split for two DISTINCT entities created without folding.
    _mk(conn, "p3", [{"type": "person", "name": "Dana West"}])
    _mk(conn, "p4", [{"type": "person", "name": "Dana North"}])   # different surname → distinct
    conn.commit()
    ei.rebuild(conn)
    ids = {r["canonical_name"]: r["id"] for r in conn.execute(
        "SELECT id, canonical_name FROM entities WHERE type='person'").fetchall()}
    r = client.post("/api/entities/split", json={"a_id": ids["Dana West"], "b_id": ids["Dana North"]})
    assert r.status_code == 200, r.text
    from app.services import entity_decisions as ed
    assert frozenset({"dana west", "dana north"}) in ed.load_splits(conn)


def test_migration_v44_to_v46_upgrade(monkeypatch):
    """An existing v44 DB upgrades in place: entity_decisions table + the new columns appear,
    and the table is writable. Guards against schema.sql/migration drift."""
    import os, sqlite3, tempfile
    dbp = os.path.join(tempfile.mkdtemp(), "up.db")
    os.environ.update(DB_PATH=dbp, JBRAIN_ACCESS_KEY="k" * 20, BRAIN_NAME="T", JBRAIN_DOMAIN="localhost")
    c = sqlite3.connect(dbp)
    c.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE notes(id INTEGER PRIMARY KEY, title TEXT UNIQUE, slug TEXT UNIQUE,"
        " content_md TEXT DEFAULT '', kind TEXT DEFAULT 'entry', created_at TEXT, updated_at TEXT, deleted_at TEXT);"
        "CREATE TABLE review_items(id INTEGER PRIMARY KEY, title TEXT, message TEXT, link_slug TEXT,"
        " status TEXT DEFAULT 'pending', created_at TEXT, dismissed_at TEXT);"
        "INSERT INTO meta(key,value) VALUES('schema_version','44');"
    )
    c.commit(); c.close()

    from app.config import get_settings
    get_settings.cache_clear()
    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()
    conn = db.get_conn()

    def cols(t):
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}

    assert db.get_meta("schema_version") == str(db.SCHEMA_VERSION)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='entity_decisions'").fetchone()
    assert "redirect_to" in cols("notes")
    assert {"kind", "payload_json"} <= cols("review_items")
    conn.execute("INSERT INTO entity_decisions(kind,type,norm_a,canonical) VALUES('merge','person','a b','c d')")
    assert conn.execute("SELECT COUNT(*) c FROM entity_decisions").fetchone()["c"] == 1


def test_merge_endpoint_round_trip(client):
    """The HTTP merge endpoint records the decision, rebuilds, and returns the survivor —
    and the merge then survives a further rebuild."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    # Distinct surnames so they stay two entities until the explicit endpoint merge.
    _mk(conn, "e1", [{"type": "person", "name": "Jeff Orozco"}])
    _mk(conn, "e2", [{"type": "person", "name": "Jeffrey Mark Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    ids = {r["normalized_key"]: r["id"] for r in conn.execute(
        "SELECT id, normalized_key FROM entities WHERE type='person'").fetchall()}
    src, into = ids["jeff orozco"], ids["jeffrey mark hopkins"]

    r = client.post("/api/entities/merge", json={"source_id": src, "into_id": into})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == into
    assert list(_persons(conn)) == ["Jeffrey Mark Hopkins"]
    ei.rebuild(conn)
    assert list(_persons(conn)) == ["Jeffrey Mark Hopkins"]


# --------------------------------------------------------------------------------------
# Phase 2: nickname/diminutive-aware person clustering.
# --------------------------------------------------------------------------------------

def test_nickname_shared_surname_auto_merges(client):
    """Jeff Hopkins + Jeffrey Mark Hopkins auto-cluster with NO decision: shared surname
    'hopkins' buckets them, jeff→jeffrey makes the mapped token sets subset."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "j1", [{"type": "person", "name": "Jeff Hopkins"}])
    _mk(conn, "j2", [{"type": "person", "name": "Jeffrey Mark Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    persons = _persons(conn)
    assert list(persons) == ["Jeffrey Mark Hopkins"]      # most-tokens wins as canonical
    assert persons["Jeffrey Mark Hopkins"] == 2
    assert sorted(ei.note_ids_for_name(conn, "Jeff Hopkins")) == \
        sorted(ei.note_ids_for_name(conn, "Jeffrey Mark Hopkins"))


def test_nickname_bob_robert_auto_merges(client):
    """Bob Smith + Robert Smith auto-cluster (bob↔robert, shared surname 'smith')."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "b1", [{"type": "person", "name": "Bob Smith"}])
    _mk(conn, "b2", [{"type": "person", "name": "Robert Smith"}])
    conn.commit()
    ei.rebuild(conn)
    assert len(_persons(conn)) == 1


def test_nickname_different_surname_does_not_merge(client):
    """Mike Jones vs Michael Smith: nickname-equivalent given names but NO shared surname,
    so they're never even compared — two distinct entities."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "d1", [{"type": "person", "name": "Mike Jones"}])
    _mk(conn, "d2", [{"type": "person", "name": "Michael Smith"}])
    conn.commit()
    ei.rebuild(conn)
    assert set(_persons(conn)) == {"Mike Jones", "Michael Smith"}


def test_nickname_suffix_conflict_blocks_merge(client):
    """Jeff Hopkins Jr + Jeffrey Hopkins Sr share surname & nickname, but a Jr/Sr
    generational-suffix conflict blocks the nickname merge."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "sx1", [{"type": "person", "name": "Jeff Hopkins Jr"}])
    _mk(conn, "sx2", [{"type": "person", "name": "Jeffrey Hopkins Sr"}])
    conn.commit()
    ei.rebuild(conn)
    # Nickname path is the only thing that could unite these (raw token sets don't subset:
    # {jeff,hopkins,jr} vs {jeffrey,hopkins,sr}); the suffix conflict blocks it.
    assert len(_persons(conn)) == 2

    # One carrying a suffix and the other NOT is also a conflict on the nickname path:
    # {bob,marsh,jr} vs {robert,marsh} don't raw-subset, so only nickname could unite them.
    _mk(conn, "sx3", [{"type": "person", "name": "Bob Marsh Jr"}])
    _mk(conn, "sx4", [{"type": "person", "name": "Robert Marsh"}])
    conn.commit()
    ei.rebuild(conn)
    persons = _persons(conn)
    assert "Bob Marsh Jr" in persons and "Robert Marsh" in persons


def test_nickname_merge_overridden_by_split(client):
    """A split still wins over a nickname auto-merge."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    _mk(conn, "ns1", [{"type": "person", "name": "Bob Marlow"}])
    _mk(conn, "ns2", [{"type": "person", "name": "Robert Marlow"}])
    conn.commit()
    ei.rebuild(conn)
    assert len(_persons(conn)) == 1                       # nickname-folded

    ed.add(conn, "split", norm_a="Bob Marlow", norm_b="Robert Marlow")
    conn.commit()
    ei.rebuild(conn)
    assert set(_persons(conn)) == {"Bob Marlow", "Robert Marlow"}
    ei.rebuild(conn)                                      # idempotent
    assert set(_persons(conn)) == {"Bob Marlow", "Robert Marlow"}


def test_nickname_merge_survives_second_rebuild(client):
    """A nickname auto-merge is stable across rebuilds."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "sr1", [{"type": "person", "name": "Liz Carmody"}])
    _mk(conn, "sr2", [{"type": "person", "name": "Elizabeth Carmody"}])
    conn.commit()
    ei.rebuild(conn)
    assert len(_persons(conn)) == 1
    ei.rebuild(conn)
    assert len(_persons(conn)) == 1


def test_nickname_does_not_affect_non_person_types(client):
    """Nickname mapping is person/animal only — an org named 'Bob' and 'Robert' sharing a
    surname-like token is NOT nickname-folded."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "o1", [{"type": "org", "name": "Bob Industries"}])
    _mk(conn, "o2", [{"type": "org", "name": "Robert Industries"}])
    conn.commit()
    ei.rebuild(conn)
    orgs = {r["canonical_name"] for r in conn.execute(
        "SELECT canonical_name FROM entities WHERE type='org'").fetchall()}
    assert orgs == {"Bob Industries", "Robert Industries"}


def test_nickname_animal_type_folds(client):
    """Animal type IS person-like for nickname purposes."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "an1", [{"type": "animal", "name": "Charlie Whiskers"}])
    _mk(conn, "an2", [{"type": "animal", "name": "Charles Whiskers"}])
    conn.commit()
    ei.rebuild(conn)
    animals = [r["canonical_name"] for r in conn.execute(
        "SELECT canonical_name FROM entities WHERE type='animal'").fetchall()]
    assert len(animals) == 1


def test_ambiguous_short_forms_do_not_auto_merge(client):
    """Different real people who share a surname but whose given names are only AMBIGUOUS
    short forms (Sandy≠Alexander, Jack≠Jonathan, Harry≠Henry, Al≠Albert) must NOT auto-merge.
    These tokens were deliberately excluded from the lexicon; this guards re-introduction."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    pairs = [("Sandy Lee", "Alexander Lee"), ("Jack Brown", "Jonathan Brown"),
             ("Harry Park", "Henry Park"), ("Al Green", "Albert Green")]
    for i, (x, y) in enumerate(pairs):
        _mk(conn, f"amb{i}a", [{"type": "person", "name": x}])
        _mk(conn, f"amb{i}b", [{"type": "person", "name": y}])
    conn.commit()
    ei.rebuild(conn)
    # Each pair stays as two distinct entities (8 people total).
    assert len([r for r in conn.execute("SELECT id FROM entities WHERE type='person'")]) == 8


def test_generational_suffix_blocks_even_exact_name(client):
    """A Jr/Sr boundary is a 'different person' signal applied consistently: even an EXACT
    same-spelled name + suffix (John Smith vs John Smith Jr) does not auto-merge — only the
    suffix distinguishes a father/son, so they stay separate until an explicit merge."""
    from app.db import get_conn
    from app.services import entity_index as ei
    conn = get_conn()
    _mk(conn, "g1", [{"type": "person", "name": "John Smith"}])
    _mk(conn, "g2", [{"type": "person", "name": "John Smith Jr"}])
    conn.commit()
    ei.rebuild(conn)
    assert len([r for r in conn.execute("SELECT id FROM entities WHERE type='person'")]) == 2


def test_surface_aliases_adds_updates_and_removes_aka_line(client):
    """surface_aliases keeps a deterministic '*Also known as: ...*' line under the H1 in sync
    with the entity's aliases — adds it, is idempotent, and removes it when aliases are gone."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed, wiki_build
    from app.services import notes as ns
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    ns.upsert_note(conn, "kb/People/Jeffrey Hopkins", "# Jeffrey Hopkins\n\nA person.", kind="kb")
    conn.commit()
    ei.rebuild(conn)                                            # links the article to the entity
    eid = conn.execute("SELECT id FROM entities WHERE normalized_key='jeffrey hopkins'").fetchone()["id"]
    assert conn.execute("SELECT article_title FROM entities WHERE id=?", (eid,)).fetchone()["article_title"] \
        == "kb/People/Jeffrey Hopkins"

    ed.add(conn, "alias", norm_a="Jeff", norm_b="jeffrey hopkins", display_a="Jeff")
    conn.commit()
    ei.rebuild(conn)
    assert wiki_build.surface_aliases(conn)["updated"] == 1
    body = conn.execute(
        "SELECT content_md FROM notes WHERE title='kb/People/Jeffrey Hopkins'").fetchone()["content_md"]
    assert "*Also known as: Jeff.*" in body
    assert body.splitlines()[0] == "# Jeffrey Hopkins"          # H1 still first

    # Idempotent: a second pass changes nothing (no blank-line accumulation).
    assert wiki_build.surface_aliases(conn)["updated"] == 0

    # Remove the alias → the line is removed.
    conn.execute("DELETE FROM entity_decisions WHERE kind='alias'")
    conn.commit()
    ei.rebuild(conn)
    assert wiki_build.surface_aliases(conn)["updated"] == 1
    body2 = conn.execute(
        "SELECT content_md FROM notes WHERE title='kb/People/Jeffrey Hopkins'").fetchone()["content_md"]
    assert "Also known as" not in body2


def test_surface_aliases_lists_only_real_aliases_not_the_canonical(client):
    """The AKA line lists aliases only — never the article's own canonical/leaf name."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed, wiki_build
    from app.services import notes as ns
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Margaret Hale"}])
    ns.upsert_note(conn, "kb/People/Margaret Hale", "# Margaret Hale\n\nA person.", kind="kb")
    conn.commit()
    ei.rebuild(conn)
    ed.add(conn, "alias", norm_a="Maggie", norm_b="margaret hale", display_a="Maggie")
    conn.commit()
    ei.rebuild(conn)
    wiki_build.surface_aliases(conn)
    body = conn.execute(
        "SELECT content_md FROM notes WHERE title='kb/People/Margaret Hale'").fetchone()["content_md"]
    assert "*Also known as: Maggie.*" in body                   # the real alias, and not the leaf
    assert "Margaret Hale" not in body.split("Also known as")[1].split("*")[0]


def test_resolve_endpoint_collapses_alias_to_canonical(client):
    """GET /api/entities/resolve?name=<alias> returns the CANONICAL entity (one person card)."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    nid = _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    ed.add(conn, "alias", norm_a="Jeff", norm_b="jeffrey hopkins", display_a="Jeff")
    conn.commit()
    ei.rebuild(conn)
    r = client.get("/api/entities/resolve", params={"name": "Jeff"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["canonical_name"] == "Jeffrey Hopkins"
    assert nid in [n["id"] for n in body["notes"]]
    # A genuinely unknown name resolves to nothing.
    assert client.get("/api/entities/resolve", params={"name": "Nobody Here"}).json() == {"resolved": None}


def test_surface_aliases_is_a_registered_pipeline_verb():
    """The recipe step must map to a primitive present in BOTH the handler and meta maps."""
    from app.services import pipeline
    assert "surface_aliases" in pipeline._PRIMITIVES
    assert "surface_aliases" in pipeline._PRIMITIVE_META


def test_apply_aka_line_robustness():
    """_apply_aka_line: frontmatter-safe (no H1-on-line-0 assumption), no-H1 is left alone,
    code-fence AKA-looking lines are preserved, alias display is markdown-escaped, idempotent."""
    from app.services import wiki_build as wb
    # Frontmatter before the H1: AKA goes under the H1, frontmatter stays on top.
    fm = "---\ntitle: X\n---\n# Real Person\n\nThe lead.\n"
    out = wb._apply_aka_line(fm, ["Nick"])
    assert out.startswith("---\ntitle: X\n---\n# Real Person\n")
    assert "*Also known as: Nick.*" in out
    assert wb._apply_aka_line(out, ["Nick"]) == out                      # idempotent
    # No H1 → unchanged (never synthesize AKA above arbitrary content).
    assert wb._apply_aka_line("no heading here\n", ["Nick"]) == "no heading here\n"
    # An AKA-looking line INSIDE a code fence must survive.
    fenced = "# P\n\nLead.\n\n```\n*Also known as: fake.*\n```\n"
    out2 = wb._apply_aka_line(fenced, ["Real"])
    assert "*Also known as: fake.*" in out2 and "*Also known as: Real.*" in out2
    # Markdown special chars in an alias are escaped.
    esc = wb._apply_aka_line("# P\n\nLead.\n", ["Bob *star*"])
    assert r"Bob \*star\*" in esc


def test_aka_line_does_not_pollute_entity_lead(client):
    """M1 regression: after surface_aliases inserts an AKA line, the entity's embed lead is the
    REAL lead sentence, not the AKA line."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed, wiki_build
    from app.services import notes as ns
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeffrey Hopkins"}])
    ns.upsert_note(conn, "kb/People/Jeffrey Hopkins",
                   "# Jeffrey Hopkins\n\nJeffrey is my neighbor and a carpenter.", kind="kb")
    conn.commit()
    ei.rebuild(conn)
    ed.add(conn, "alias", norm_a="Jeff", norm_b="jeffrey hopkins", display_a="Jeff")
    conn.commit()
    ei.rebuild(conn)
    wiki_build.surface_aliases(conn)
    leads = ei._article_leads(conn)
    assert leads["kb/People/Jeffrey Hopkins"].startswith("Jeffrey is my neighbor")
    assert "Also known as" not in leads["kb/People/Jeffrey Hopkins"]
