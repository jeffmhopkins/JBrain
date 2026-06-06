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
    both notes and the other name becomes an alias. Idempotent across a second rebuild."""
    from app.db import get_conn
    from app.services import entity_index as ei, entity_decisions as ed
    conn = get_conn()
    _mk(conn, "n1", [{"type": "person", "name": "Jeff Hopkins"}])
    _mk(conn, "n2", [{"type": "person", "name": "Jeffrey Mark Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    # Heuristic alone: they DON'T fold (no shared distinctive surname-subset relationship
    # that subsumes — "jeff hopkins" vs "jeffrey mark hopkins" don't subset on tokens).
    assert len(_persons(conn)) == 2

    # Merge "Jeff Hopkins" into "Jeffrey Mark Hopkins".
    ed.add(conn, "merge", norm_a="Jeff Hopkins", canonical="Jeffrey Mark Hopkins")
    conn.commit()
    ei.rebuild(conn)
    persons = _persons(conn)
    assert list(persons) == ["Jeffrey Mark Hopkins"]
    assert persons["Jeffrey Mark Hopkins"] == 2          # both notes folded in
    # The merged-away name is an alias and resolves to the survivor's notes.
    survivor = conn.execute(
        "SELECT id FROM entities WHERE normalized_key='jeffrey mark hopkins'").fetchone()["id"]
    aliases = [a["alias_norm"] for a in conn.execute(
        "SELECT alias_norm FROM entity_aliases WHERE entity_id=?", (survivor,)).fetchall()]
    assert "jeff hopkins" in aliases
    assert sorted(ei.note_ids_for_name(conn, "Jeff Hopkins")) == \
        sorted(ei.note_ids_for_name(conn, "Jeffrey Mark Hopkins"))

    # (2) Idempotency — rebuild AGAIN; still one merged entity (the core regression).
    ei.rebuild(conn)
    persons2 = _persons(conn)
    assert list(persons2) == ["Jeffrey Mark Hopkins"] and persons2["Jeffrey Mark Hopkins"] == 2


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


def test_migration_v44_to_v45_upgrade(monkeypatch):
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

    assert db.get_meta("schema_version") == "45"
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
    _mk(conn, "e1", [{"type": "person", "name": "Jeff Hopkins"}])
    _mk(conn, "e2", [{"type": "person", "name": "Jeffrey Mark Hopkins"}])
    conn.commit()
    ei.rebuild(conn)
    ids = {r["normalized_key"]: r["id"] for r in conn.execute(
        "SELECT id, normalized_key FROM entities WHERE type='person'").fetchall()}
    src, into = ids["jeff hopkins"], ids["jeffrey mark hopkins"]

    r = client.post("/api/entities/merge", json={"source_id": src, "into_id": into})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == into
    assert list(_persons(conn)) == ["Jeffrey Mark Hopkins"]
    ei.rebuild(conn)
    assert list(_persons(conn)) == ["Jeffrey Mark Hopkins"]
