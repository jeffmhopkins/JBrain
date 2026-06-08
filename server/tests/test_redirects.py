"""Phase 3 — article redirects + declutter.

A merged-away person article is CONVERTED IN PLACE to a live redirect row (keeps its
UNIQUE title slot) so old [[links]] / external URLs still resolve, while it's hidden from
browse, the index, dead-link scans, the entity index, and keyword search.

Embedding calls are monkeypatched (same as test_api) so the suite runs without the model.
"""
import os
import tempfile

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")
pytest.importorskip("anthropic")

pytestmark = pytest.mark.integration

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


def _mk(conn, title, body):
    from app.services import notes as ns
    ns.upsert_note(conn, title, body, kind="kb")


def test_create_redirect_converts_in_place(client):
    """create_redirect on an existing row converts it IN PLACE: still live, redirect_to set,
    body replaced with the one-line marker, slug preserved."""
    from app.db import get_conn
    from app.services import wiki_build
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nThe canonical Bob.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nA second Bob page.")
    conn.commit()
    slug_before = conn.execute("SELECT slug FROM notes WHERE title='kb/People/Bobby'").fetchone()["slug"]
    res = wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")
    conn.commit()
    assert res["ok"] and res["to"] == "kb/People/Bob"
    row = conn.execute(
        "SELECT slug, deleted_at, redirect_to, content_md, kind FROM notes WHERE title='kb/People/Bobby'"
    ).fetchone()
    assert row["deleted_at"] is None                       # kept LIVE (title slot held)
    assert row["redirect_to"] == "kb/People/Bob"
    assert row["kind"] == "kb"
    assert row["content_md"] == "Redirects to [[kb/People/Bob]]."
    assert row["slug"] == slug_before                      # slug preserved


def test_inbound_links_to_redirected_title_still_resolve(client):
    """An inbound [[from]] link re-points at the redirect row, so its backlink keeps working."""
    from app.db import get_conn
    from app.services import wiki_build
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nThe canonical Bob.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nSecond page.")
    _mk(conn, "kb/People/Carol", "# Carol\nKnows [[kb/People/Bobby]].")
    conn.commit()
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")
    conn.commit()
    nid = conn.execute("SELECT id FROM notes WHERE title='kb/People/Bobby'").fetchone()["id"]
    link = conn.execute(
        "SELECT target_note_id FROM links WHERE lower(target_title)='kb/people/bobby'"
    ).fetchone()
    assert link and link["target_note_id"] == nid          # link still resolves to the live row


def test_get_redirect_slug_returns_redirect_to(client):
    """GET /api/notes/<redirect-slug> returns redirect_to + the resolved redirect_to_slug."""
    from app.db import get_conn
    from app.services import wiki_build
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nThe canonical Bob.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nSecond page.")
    conn.commit()
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")
    conn.commit()
    from_slug = conn.execute("SELECT slug FROM notes WHERE title='kb/People/Bobby'").fetchone()["slug"]
    to_slug = conn.execute("SELECT slug FROM notes WHERE title='kb/People/Bob'").fetchone()["slug"]
    r = client.get(f"/api/notes/{from_slug}").json()
    assert r["redirect_to"] == "kb/People/Bob"
    assert r["redirect_to_slug"] == to_slug


def test_redirect_chain_resolves_to_final_target(client):
    """A redirect targeting a redirect points at the FINAL target, not the middle hop."""
    from app.db import get_conn
    from app.services import wiki_build
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nCanonical.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nSecond.")
    _mk(conn, "kb/People/Rob", "# Rob\nThird.")
    conn.commit()
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")   # Bobby -> Bob
    conn.commit()
    wiki_build.create_redirect(conn, "kb/People/Rob", "kb/People/Bobby")   # Rob -> (Bobby ->) Bob
    conn.commit()
    rob = conn.execute("SELECT redirect_to FROM notes WHERE title='kb/People/Rob'").fetchone()
    assert rob["redirect_to"] == "kb/People/Bob"            # collapsed to the final target


def test_self_and_cycle_refused(client):
    """A self-redirect is refused; a redirect that would close a cycle is refused too."""
    from app.db import get_conn
    from app.services import wiki_build
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nCanonical.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nSecond.")
    conn.commit()
    assert not wiki_build.create_redirect(conn, "kb/People/Bob", "kb/People/Bob")["ok"]
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")    # Bobby -> Bob
    conn.commit()
    # Bob -> Bobby would resolve through Bobby's chain back to Bob == self → refused.
    assert not wiki_build.create_redirect(conn, "kb/People/Bob", "kb/People/Bobby")["ok"]


def test_redirect_decluttered_everywhere(client):
    """The redirect row drops out of list_notes, _known_titles, refresh_index, and
    dead_links — and the entity index routes the subject to the CANONICAL article."""
    from app.db import get_conn
    from app.services import wiki_build, entity_index
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nCanonical Bob.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nSecond page.")
    conn.commit()
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")
    conn.commit()

    # list_notes (browse) excludes it.
    titles = {n["title"] for n in client.get("/api/notes?include_hidden=true").json()}
    assert "kb/People/Bob" in titles and "kb/People/Bobby" not in titles

    # _known_titles excludes it.
    kt = wiki_build._known_titles(conn)
    assert "kb/People/Bob" in kt and "kb/People/Bobby" not in kt

    # refresh_index leaves the redirect out of the org map.
    wiki_build.refresh_index(conn)
    conn.commit()
    idx = conn.execute("SELECT content_md FROM notes WHERE title='kb/_index'").fetchone()["content_md"]
    assert "[[kb/People/Bob]]" in idx and "kb/People/Bobby" not in idx

    # dead_links ignores the redirect marker's own [[link]] even if it dangled.
    assert all(d["source_title"] != "kb/People/Bobby" for d in wiki_build.dead_links(conn))

    # entity index: a Bobby entity (note that mentions "Bobby") routes to the redirect's
    # canonical leaf article, never the redirect row. The leaf_map for _link_articles
    # excludes redirects, so a "Bobby" article leaf simply isn't offered.
    leaf_map_titles = set()
    for k in conn.execute("SELECT title FROM notes WHERE kind='kb' AND deleted_at IS NULL AND redirect_to IS NULL"):
        leaf_map_titles.add(k["title"])
    assert "kb/People/Bobby" not in leaf_map_titles


def test_entity_points_to_canonical_not_redirect(client, monkeypatch):
    """_link_articles links a subject to the live canonical article, never to a same-leaf
    redirect row."""
    from app.db import get_conn
    from app.services import wiki_build, entity_index
    import json
    conn = get_conn()
    # A source note whose analysis names the entity "Grover".
    from app.services import notes as ns
    nid = ns.upsert_note(conn, "notes/g1", "Grover is a cat.")
    conn.execute(
        "INSERT INTO note_analysis (note_id, gist, domain, entities_json, content_hash) VALUES (?,?,?,?,?)",
        (nid, "a cat", "Animals", json.dumps([{"name": "Grover", "type": "animal"}]), "h"),
    )
    _mk(conn, "kb/Things/Grover", "# Grover\nThe canonical cat.")
    _mk(conn, "kb/Things/GroverCat", "# Grover\nAnother cat page.")
    conn.commit()
    wiki_build.create_redirect(conn, "kb/Things/GroverCat", "kb/Things/Grover")
    conn.commit()
    entity_index.rebuild(conn)
    art = conn.execute(
        "SELECT article_title FROM entities WHERE normalized_key='grover'"
    ).fetchone()
    assert art and art["article_title"] == "kb/Things/Grover"   # canonical, not the redirect


def test_redirect_not_a_keyword_hit(client):
    """A keyword search for the redirected title finds nothing for the redirect ROW: its
    FTS entry is dropped on conversion (the canonical article remains searchable)."""
    from app.db import get_conn
    from app.services import wiki_build
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nCanonical Bob.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nThe Bobby page mentions Bobby plenty.")
    conn.commit()
    nid = conn.execute("SELECT id FROM notes WHERE title='kb/People/Bobby'").fetchone()["id"]
    assert conn.execute("SELECT 1 FROM notes_fts WHERE note_id=?", (nid,)).fetchone()  # indexed pre-convert
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")
    conn.commit()
    # No FTS row remains for the redirect, so a MATCH on its title can't surface it.
    assert not conn.execute("SELECT 1 FROM notes_fts WHERE note_id=?", (nid,)).fetchone()
    hits = conn.execute(
        "SELECT n.title FROM notes_fts f JOIN notes n ON n.id=f.note_id "
        "WHERE notes_fts MATCH 'Bobby' AND n.deleted_at IS NULL"
    ).fetchall()
    assert all(h["title"] != "kb/People/Bobby" for h in hits)


def test_redirect_survives_reset(client):
    """reset() (full rebuild) SKIPS redirect rows so a merge survives — old links keep
    resolving — and counts them as kept, not deleted."""
    from app.db import get_conn
    from app.services import wiki_build
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nCanonical.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nSecond.")
    conn.commit()
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")
    conn.commit()
    res = wiki_build.reset(conn)
    conn.commit()
    row = conn.execute(
        "SELECT deleted_at, redirect_to FROM notes WHERE title='kb/People/Bobby'"
    ).fetchone()
    assert row["deleted_at"] is None and row["redirect_to"] == "kb/People/Bob"  # not soft-deleted
    assert res["kept"] >= 1


def test_create_redirect_inserts_new_row_when_no_existing(client):
    """With no row titled from_title, create_redirect INSERTS a fresh kb redirect row."""
    from app.db import get_conn
    from app.services import wiki_build
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nCanonical.")
    conn.commit()
    res = wiki_build.create_redirect(conn, "kb/People/OldName", "kb/People/Bob")
    conn.commit()
    assert res["ok"]
    row = conn.execute(
        "SELECT kind, deleted_at, redirect_to, content_md FROM notes WHERE title='kb/People/OldName'"
    ).fetchone()
    assert row and row["kind"] == "kb" and row["deleted_at"] is None
    assert row["redirect_to"] == "kb/People/Bob"
    assert row["content_md"] == "Redirects to [[kb/People/Bob]]."


def test_real_content_upsert_clears_redirect(client):
    """BLOCKER guard: a normal content write to a redirected title supersedes the redirect —
    redirect_to is cleared, FTS is restored, and it reappears in browse (no FTS/browse vs
    redirect 'zombie' state)."""
    from app.db import get_conn
    from app.services import wiki_build
    from app.services import notes as ns
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nCanonical.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nSecond page.")
    conn.commit()
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")
    conn.commit()
    # A later real write re-creates an article at that title (e.g. a rebuild).
    ns.upsert_note(conn, "kb/People/Bobby", "# Bobby\nReal content again.", kind="kb")
    conn.commit()
    row = conn.execute("SELECT id, redirect_to FROM notes WHERE title='kb/People/Bobby'").fetchone()
    assert row["redirect_to"] is None                          # redirect flag cleared
    assert conn.execute("SELECT 1 FROM notes_fts WHERE note_id=?", (row["id"],)).fetchone()  # re-indexed
    titles = {n["title"] for n in client.get("/api/notes?kind=kb&limit=500").json()}
    assert "kb/People/Bobby" in titles                         # back in browse


def test_create_article_does_not_fold_into_a_redirect(client):
    """create_article must NOT route a new subject into a merged-away redirect title."""
    from app.db import get_conn
    from app.services import wiki_build
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nCanonical.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nSecond.")
    conn.commit()
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")
    conn.commit()
    res = wiki_build.create_article(conn, "Bobby")
    # Must not return a fold whose target is the redirect row.
    assert not (res.get("folded") and res.get("title") == "kb/People/Bobby")


def test_graph_excludes_redirect_nodes(client):
    """A redirect must not appear as a node in the knowledge graph."""
    from app.db import get_conn
    from app.services import wiki_build
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nCanonical.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nSecond.")
    conn.commit()
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")
    conn.commit()
    nodes = {n["title"] for n in client.get("/api/graph").json()["nodes"]}
    assert "kb/People/Bob" in nodes and "kb/People/Bobby" not in nodes


def test_reindex_skips_redirect_rows(client, monkeypatch):
    """reindex_missing_note_chunks must not re-embed a redirect's marker (semantic leak)."""
    from app.db import get_conn
    from app.services import wiki_build, embeddings
    conn = get_conn()
    _mk(conn, "kb/People/Bob", "# Bob\nCanonical.")
    _mk(conn, "kb/People/Bobby", "# Bobby\nSecond.")
    conn.commit()
    wiki_build.create_redirect(conn, "kb/People/Bobby", "kb/People/Bob")
    conn.commit()
    redirect_id = conn.execute("SELECT id FROM notes WHERE title='kb/People/Bobby'").fetchone()["id"]
    seen: list[int] = []
    monkeypatch.setattr(embeddings, "upsert_note_chunk_embeddings",
                        lambda c, nid, text: seen.append(nid))
    embeddings.reindex_missing_note_chunks(conn)
    assert redirect_id not in seen                             # redirect never re-embedded
