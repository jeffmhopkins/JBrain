"""Integration tests for access-key auth, notes, wiki-links, and staging.

Embedding calls are monkeypatched so the suite runs without downloading the
local model. Skipped automatically if native deps aren't installed.
"""
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

    import app.db as db
    db._initialized = False
    db._local.__dict__.clear()
    db.init_db()

    from app import auth
    auth.ensure_access_key()  # seed the key hash from the env

    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})


def test_health_is_public(client):
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    assert anon.get("/api/health").json()["ok"] is True
    assert anon.get("/api/auth/info").json()["brain_name"] == "Test Brain"


def test_rejects_missing_key():
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app)
    assert anon.get("/api/notes").status_code == 401


def test_rejects_wrong_key():
    from fastapi.testclient import TestClient
    from app.main import app
    bad = TestClient(app, headers={"Authorization": "Bearer nope"})
    assert bad.get("/api/notes").status_code == 401


def test_verify_with_valid_key(client):
    assert client.get("/api/auth/verify").json()["ok"] is True


def test_create_note_and_backlinks(client):
    client.post("/api/notes", json={"title": "Alpha", "content_md": "links to [[Beta]]"})
    client.post("/api/notes", json={"title": "Beta", "content_md": "the beta note"})

    beta = client.get("/api/notes/beta").json()
    assert beta["title"] == "Beta"
    assert any(b["title"] == "Alpha" for b in beta["backlinks"])


def test_versioning_timeline(client):
    client.post("/api/notes", json={"title": "Gamma", "content_md": "v1"})
    client.post("/api/notes", json={"title": "Gamma", "content_md": "v2"})
    timeline = client.get("/api/notes/gamma/versions").json()
    # Newest first; newest is current and equals live content.
    assert timeline[0]["is_current"] is True
    assert len(timeline) >= 2
    newest = client.get(f"/api/notes/gamma/versions/{timeline[0]['version_id']}").json()
    oldest = client.get(f"/api/notes/gamma/versions/{timeline[-1]['version_id']}").json()
    assert newest["content_md"] == "v2"
    assert oldest["content_md"] == "v1"


def test_diff_and_restore(client):
    client.post("/api/notes", json={"title": "Delta", "content_md": "line one\nline two"})
    client.post("/api/notes", json={"title": "Delta", "content_md": "line one\nline THREE"})
    tl = client.get("/api/notes/delta/versions").json()
    cur, prev = tl[0]["version_id"], tl[-1]["version_id"]

    diff = client.get(f"/api/notes/delta/diff/{prev}/{cur}").json()
    types = {h["type"] for h in diff["hunks"]}
    assert "insert" in types and "delete" in types

    # Restore the original; live content should revert, history preserved.
    client.post("/api/notes/delta/restore", json={"version_id": prev})
    assert client.get("/api/notes/delta").json()["content_md"] == "line one\nline two"
    after = client.get("/api/notes/delta/versions").json()
    assert len(after) == len(tl) + 1  # restore added a new version, nothing destroyed
    assert after[0]["source"] == "restore"


def test_architect_edits_attributed(client):
    import json as _json
    from app.db import get_conn
    conn = get_conn()
    conn.execute(
        "INSERT INTO conversations (id, title) VALUES (99, 't')"
    )
    conn.execute(
        "INSERT INTO staging_actions (conversation_id, type, payload_json) VALUES (99, 'CREATE', ?)",
        (_json.dumps({"type": "CREATE", "title": "FromAI", "content": "x", "summary": "s"}),),
    )
    conn.commit()
    pending = client.get("/api/staging").json()
    client.post(f"/api/staging/{pending[0]['id']}/apply")
    tl = client.get("/api/notes/fromai/versions").json()
    assert tl[0]["source"] == "architect"
    assert tl[0]["conversation_id"] == 99


def test_sql_console_rejects_writes(client):
    assert client.post("/api/sql", json={"sql": "DELETE FROM notes"}).status_code == 400
    assert client.post("/api/sql", json={"sql": "SELECT count(*) FROM notes"}).status_code == 200


def test_staging_apply(client):
    import json as _json
    from app.db import get_conn
    conn = get_conn()
    conn.execute(
        "INSERT INTO staging_actions (type, payload_json) VALUES ('CREATE', ?)",
        (_json.dumps({"type": "CREATE", "title": "Staged", "content": "hi", "summary": "s"}),),
    )
    conn.commit()

    pending = client.get("/api/staging").json()
    assert len(pending) == 1
    client.post(f"/api/staging/{pending[0]['id']}/apply")
    assert client.get("/api/notes/staged").json()["title"] == "Staged"


def test_attachments_upload_list_delete(client):
    client.post("/api/notes", json={"title": "Host", "content_md": "host note"})

    up = client.post(
        "/api/notes/host/attachments",
        files={"file": ("spec.md", b"# Spec\n\nsome searchable content", "text/markdown")},
    )
    assert up.status_code == 200
    att_id = up.json()["id"]

    listing = client.get("/api/notes/host/attachments").json()
    assert any(a["filename"] == "spec.md" for a in listing)

    got = client.get(f"/api/attachments/{att_id}").json()
    assert "searchable content" in got["content_text"]

    # FTS keyword search should surface the attachment.
    res = client.get("/api/search?q=searchable&mode=keyword").json()
    assert any(r["kind"] == "attachment" and r["attachment_id"] == att_id for r in res)

    assert client.delete(f"/api/attachments/{att_id}").status_code == 200
    assert client.get("/api/notes/host/attachments").json() == []


def test_attachments_rejects_non_text(client):
    client.post("/api/notes", json={"title": "Host2", "content_md": "x"})
    bad = client.post(
        "/api/notes/host2/attachments",
        files={"file": ("image.png", b"\x89PNG\r\n", "image/png")},
    )
    assert bad.status_code == 415
