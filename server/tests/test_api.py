"""Integration tests for notes, wiki-links, backlinks, staging, and auth.

Embedding calls are monkeypatched so the suite runs without downloading the
local model. Skipped automatically if native deps aren't installed.
"""
import os
import tempfile

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("fastapi")
pytest.importorskip("anthropic")


@pytest.fixture()
def client(monkeypatch):
    # Isolated temp DB + known admin creds before anything imports settings.
    tmp = tempfile.mkdtemp()
    os.environ.update(
        DB_PATH=os.path.join(tmp, "test.db"),
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="secret",
        SESSION_SECRET="test-secret",
        BRAIN_NAME="Test Brain",
        JBRAIN_DOMAIN="localhost",
    )
    from app.config import get_settings
    get_settings.cache_clear()

    # Neutralise the embedding model (no network / no heavy download).
    from app.services import embeddings
    monkeypatch.setattr(embeddings, "upsert_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "delete_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "semantic_search", lambda *a, **k: [])

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()

    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    return c


def test_health(client):
    assert client.get("/api/health").json()["ok"] is True


def test_requires_auth():
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    assert anon.get("/api/notes").status_code == 401


def test_create_note_and_backlinks(client):
    client.post("/api/notes", json={"title": "Alpha", "content_md": "links to [[Beta]]"})
    client.post("/api/notes", json={"title": "Beta", "content_md": "the beta note"})

    beta = client.get("/api/notes/beta").json()
    assert beta["title"] == "Beta"
    # Alpha links to Beta -> Beta should show Alpha as a backlink.
    assert any(b["title"] == "Alpha" for b in beta["backlinks"])


def test_versioning_on_update(client):
    client.post("/api/notes", json={"title": "Gamma", "content_md": "v1"})
    client.post("/api/notes", json={"title": "Gamma", "content_md": "v2"})
    versions = client.get("/api/notes/gamma/versions").json()
    assert any(v["content_md"] == "v1" for v in versions)


def test_sql_console_rejects_writes(client):
    bad = client.post("/api/sql", json={"sql": "DELETE FROM notes"})
    assert bad.status_code == 400
    ok = client.post("/api/sql", json={"sql": "SELECT count(*) FROM notes"})
    assert ok.status_code == 200


def test_staging_apply(client):
    conn_resp = client.post("/api/sql", json={"sql": "SELECT 1"})
    assert conn_resp.status_code == 200
    # Manually stage a CREATE (mimicking the architect) via a direct insert path:
    from app.db import get_conn
    import json as _json
    conn = get_conn()
    conn.execute(
        "INSERT INTO staging_actions (type, payload_json) VALUES ('CREATE', ?)",
        (_json.dumps({"type": "CREATE", "title": "Staged", "content": "hi", "summary": "s"}),),
    )
    conn.commit()

    pending = client.get("/api/staging").json()
    assert len(pending) == 1
    action_id = pending[0]["id"]

    client.post(f"/api/staging/{action_id}/apply")
    note = client.get("/api/notes/staged").json()
    assert note["title"] == "Staged"
