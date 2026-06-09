"""Autonomous taxonomy reorg: deterministic-first foldering of un-foldered Reference pages.

Embeddings are stubbed (``semantic_search`` -> ``[]``) so placement is driven by the
deterministic link-corroboration signal; the LLM is mocked at the ``app.services.llm`` seam.
The index machinery (entity rebuild / dead-link sweep / index refresh) is stubbed to isolate
the reorg's SELECTION, CAP, COOLDOWN and CONVERGENCE logic from ``recategorize_article``'s
move plumbing (which is covered by ``test_corrections``).
"""
import os
import tempfile

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")
pytest.importorskip("anthropic")

pytestmark = pytest.mark.integration

TEST_KEY = "test-access-key-1234567890"


def _boom(*_a, **_k):
    """Stand-in for ``llm.complete`` that fails if the confident path ever calls it."""
    raise AssertionError("LLM must not be called on the confident/dry-run path")


@pytest.fixture()
def env(monkeypatch):
    """Bring up an isolated DB + authed TestClient, with embeddings and the KB index stubbed."""
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

    # Isolate from the entity/index machinery — recategorize_article's move plumbing is
    # covered by test_corrections; here we exercise reorg selection/caps/convergence.
    from app.services import wiki_build, entity_index
    monkeypatch.setattr(entity_index, "rebuild", lambda *a, **k: 0)
    monkeypatch.setattr(wiki_build, "flag_dead_links", lambda *a, **k: None)
    monkeypatch.setattr(wiki_build, "refresh_index", lambda *a, **k: None)

    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})
    return client, db.get_conn()


def _mk(client, title, body=None):
    """Create a kb article via the API (the kb/ prefix sets kind='kb'; links are extracted)."""
    body = body or f"# {title.rsplit('/', 1)[-1]}\n\nContent for {title}."
    r = client.post("/api/notes", json={"title": title, "content_md": body}).json()
    assert r["title"] == title, r
    return r


def _log_recat(conn, pair_key, at_expr="CAST(strftime('%s','now') AS INTEGER)"):
    conn.execute("CREATE TABLE IF NOT EXISTS kb_structure_log (op TEXT, pair_key TEXT, at INTEGER)")
    conn.execute(f"INSERT INTO kb_structure_log(op, pair_key, at) VALUES('recat', ?, {at_expr})", (pair_key,))
    conn.commit()


def test_confident_move_folds_into_corroborated_subcategory(env, monkeypatch):
    client, conn = env
    from app.services import wiki_build, llm, notes as notes_svc
    _mk(client, "kb/Reference/Medications/Ibuprofen")                 # an existing subcategory
    _mk(client, "kb/Reference/Aspirin",                               # flat page linking a member
        "# Aspirin\n\nRelated to [[kb/Reference/Medications/Ibuprofen]].")
    monkeypatch.setattr(llm, "complete", _boom)                      # confident path spends no LLM

    out = wiki_build.reorg_taxonomy(conn, max_moves=5)

    assert out["llm_used"] == 0
    assert out["moved"] == [{"from": "kb/Reference/Aspirin", "to": "kb/Reference/Medications/Aspirin"}]
    assert notes_svc.get_by_title(conn, "kb/Reference/Aspirin") is None
    assert notes_svc.get_by_title(conn, "kb/Reference/Medications/Aspirin") is not None
    assert conn.execute("SELECT 1 FROM kb_structure_log WHERE op='recat' AND pair_key=?",
                        ("kb/Reference/Aspirin|kb/Reference/Medications/Aspirin",)).fetchone()

    # Convergence: a second pass finds nothing left to fold.
    assert wiki_build.reorg_taxonomy(conn, max_moves=5)["moved"] == []


def test_unplaceable_page_is_carded_not_moved(env, monkeypatch):
    client, conn = env
    from app.services import wiki_build, llm, notes as notes_svc
    _mk(client, "kb/Reference/Medications/Ibuprofen")
    _mk(client, "kb/Reference/Aspirin")                              # no links, no embedding signal
    monkeypatch.setattr(llm, "has_credentials", lambda: False)

    out = wiki_build.reorg_taxonomy(conn, max_moves=5)

    assert out["moved"] == []
    assert "kb/Reference/Aspirin" in out["carded"]
    assert notes_svc.get_by_title(conn, "kb/Reference/Aspirin") is not None


def test_cooldown_skips_recently_moved_page(env):
    client, conn = env
    from app.services import wiki_build
    _mk(client, "kb/Reference/Medications/Ibuprofen")
    _mk(client, "kb/Reference/Aspirin", "# Aspirin\n\n[[kb/Reference/Medications/Ibuprofen]]")
    _log_recat(conn, "kb/Reference/Aspirin|kb/Reference/Somewhere")  # touched recently

    out = wiki_build.reorg_taxonomy(conn, max_moves=5, cooldown_days=7)

    assert out["moved"] == []
    assert {"title": "kb/Reference/Aspirin", "reason": "cooldown"} in out["skipped"]


def test_inverse_move_permanently_blocked(env):
    client, conn = env
    from app.services import wiki_build
    _mk(client, "kb/Reference/Medications/Ibuprofen")
    _mk(client, "kb/Reference/Aspirin", "# Aspirin\n\n[[kb/Reference/Medications/Ibuprofen]]")
    # A long-ago move of Medications/Aspirin -> Aspirin (at=0 dodges the cooldown window) blocks
    # the inverse forever, so the page can't oscillate back.
    _log_recat(conn, "kb/Reference/Medications/Aspirin|kb/Reference/Aspirin", at_expr="0")

    out = wiki_build.reorg_taxonomy(conn, max_moves=5)

    assert out["moved"] == []
    assert {"title": "kb/Reference/Aspirin", "reason": "inverse-blocked"} in out["skipped"]


def test_max_moves_caps_per_run(env, monkeypatch):
    client, conn = env
    from app.services import wiki_build, llm
    _mk(client, "kb/Reference/Medications/Ibuprofen")
    for name in ("Aspirin", "Naproxen", "Paracetamol"):
        _mk(client, f"kb/Reference/{name}", f"# {name}\n\n[[kb/Reference/Medications/Ibuprofen]]")
    monkeypatch.setattr(llm, "complete", _boom)

    out = wiki_build.reorg_taxonomy(conn, max_moves=1)

    assert len(out["moved"]) == 1


def test_llm_tiebreaks_ambiguous_placement(env, monkeypatch):
    client, conn = env
    from app.services import wiki_build, llm
    _mk(client, "kb/Reference/Medications/Ibuprofen")
    _mk(client, "kb/Reference/Chemistry/Acetyl")
    _mk(client, "kb/Reference/Aspirin",                              # links BOTH subs → a tie
        "# Aspirin\n\n[[kb/Reference/Medications/Ibuprofen]] and [[kb/Reference/Chemistry/Acetyl]].")
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: '{"subfolder": "kb/Reference/Medications"}')

    out = wiki_build.reorg_taxonomy(conn, max_moves=5)

    assert out["llm_used"] == 1
    assert out["moved"] == [{"from": "kb/Reference/Aspirin", "to": "kb/Reference/Medications/Aspirin"}]


def test_llm_hallucinated_subfolder_is_rejected(env, monkeypatch):
    client, conn = env
    from app.services import wiki_build, llm, notes as notes_svc
    _mk(client, "kb/Reference/Medications/Ibuprofen")
    _mk(client, "kb/Reference/Chemistry/Acetyl")
    _mk(client, "kb/Reference/Aspirin",
        "# Aspirin\n\n[[kb/Reference/Medications/Ibuprofen]] and [[kb/Reference/Chemistry/Acetyl]].")
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: '{"subfolder": "kb/_admin"}')  # not a candidate

    out = wiki_build.reorg_taxonomy(conn, max_moves=5)

    assert out["moved"] == []
    assert "kb/Reference/Aspirin" in out["carded"]
    assert notes_svc.get_by_title(conn, "kb/Reference/Aspirin") is not None


def test_dry_run_reports_without_applying(env, monkeypatch):
    client, conn = env
    from app.services import wiki_build, llm, notes as notes_svc
    _mk(client, "kb/Reference/Medications/Ibuprofen")
    _mk(client, "kb/Reference/Aspirin", "# Aspirin\n\n[[kb/Reference/Medications/Ibuprofen]]")
    monkeypatch.setattr(llm, "complete", _boom)

    out = wiki_build.reorg_taxonomy(conn, max_moves=5, dry_run=True)

    assert out["moved"] == [{"from": "kb/Reference/Aspirin",
                             "to": "kb/Reference/Medications/Aspirin", "dry_run": True}]
    assert notes_svc.get_by_title(conn, "kb/Reference/Aspirin") is not None        # untouched
    assert not conn.execute("SELECT 1 FROM kb_structure_log WHERE op='recat'").fetchone()
