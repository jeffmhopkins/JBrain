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


def test_quicktask_add_list_item_and_undo(client):
    import json as _json
    from app.db import get_conn
    from app.services import quicktasks
    conn = get_conn()
    r = quicktasks.add_list_item(conn, "Shopping List", "milk")
    conn.commit()
    assert "- [ ] milk" in client.get("/api/notes/shopping-list").json()["content_md"]

    # Record the applied op with its inverse (as the architect would), then undo.
    cur = conn.execute(
        "INSERT INTO staging_actions (type, payload_json, status) VALUES ('ADD_ITEM', ?, 'applied')",
        (_json.dumps({"summary": "x", "undo": {"op": "remove_line", "title": "Shopping List", "line": r["line"]}}),),
    )
    conn.commit()
    client.post(f"/api/staging/{cur.lastrowid}/undo")
    assert "- [ ] milk" not in client.get("/api/notes/shopping-list").json()["content_md"]


def test_quicktask_log_entry(client):
    from app.db import get_conn
    from app.services import quicktasks
    conn = get_conn()
    quicktasks.append_log(conn, "Running Log", "5k easy", date="2026-05-31")
    conn.commit()
    body = client.get("/api/notes/running-log").json()["content_md"]
    assert "5k easy" in body and "2026-05-31" in body


def test_capture_inbox_and_undo(client):
    import json as _json
    from app.db import get_conn
    from app.services import quicktasks
    conn = get_conn()
    iid = quicktasks.capture_inbox(conn, "remember milk")
    conn.commit()
    assert any(i["id"] == iid for i in client.get("/api/capture").json())

    cur = conn.execute(
        "INSERT INTO staging_actions (type, payload_json, status) VALUES ('CAPTURE', ?, 'applied')",
        (_json.dumps({"summary": "x", "undo": {"op": "delete_inbox", "id": iid}}),),
    )
    conn.commit()
    client.post(f"/api/staging/{cur.lastrowid}/undo")
    assert not any(i["id"] == iid for i in client.get("/api/capture").json())


def test_attachments_rejects_non_text(client):
    client.post("/api/notes", json={"title": "Host2", "content_md": "x"})
    bad = client.post(
        "/api/notes/host2/attachments",
        files={"file": ("image.png", b"\x89PNG\r\n", "image/png")},
    )
    assert bad.status_code == 415


def _write_workflow(tmp, name, body):
    import os
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, name), "w") as fh:
        fh.write(body)


def test_workflow_ingest_and_event_trigger(client, monkeypatch, tmp_path):
    from app.db import get_conn
    from app.services import workflows as wf_svc
    _write_workflow(str(tmp_path), "evt.yaml", """
key: test-evt
name: Test event
enabled: true
trigger:
  type: event
  event: log_appended
action:
  type: append_to_note
  config:
    title: Workflow Output
    text: "fired"
""")
    monkeypatch.setenv("JBRAIN_WORKFLOWS_DIR", str(tmp_path))
    conn = get_conn()
    assert wf_svc.ingest_repo_workflows(conn) == 1
    # Re-ingest is idempotent (hash unchanged -> no further upserts).
    assert wf_svc.ingest_repo_workflows(conn) == 0

    wf_svc.fire_event(conn, "log_appended", {"note_title": "Running Log"})
    conn.commit()
    assert "fired" in client.get("/api/notes/workflow-output").json()["content_md"]


def test_workflow_scheduled_due(client):
    from app.db import get_conn
    from app.services import workflows as wf_svc
    conn = get_conn()
    conn.execute(
        "INSERT INTO workflows (key, name, trigger_type, trigger_config, action_type, "
        "action_config, enabled) VALUES ('sched','S','schedule', ?, 'append_to_note', ?, 1)",
        ('{"interval_seconds": 3600}', '{"title": "Sched Out", "text": "tick"}'),
    )
    conn.commit()
    assert wf_svc.run_due_scheduled(conn) == 1  # never run -> due
    assert "tick" in client.get("/api/notes/sched-out").json()["content_md"]
    # Just ran -> not due again.
    assert wf_svc.run_due_scheduled(conn) == 0


def test_workflow_crud_via_api(client):
    created = client.post("/api/workflows", json={
        "name": "Manual", "trigger_type": "event",
        "trigger_config": {"event": "noop"}, "action_type": "append_to_note",
        "action_config": {"title": "Manual Out", "text": "hello"}, "enabled": True,
    }).json()
    wid = created["id"]
    assert created["locked"] is True  # user-created -> locked from repo re-ingest

    run = client.post(f"/api/workflows/{wid}/run").json()
    assert run["status"] == "ok"
    assert "hello" in client.get("/api/notes/manual-out").json()["content_md"]

    toggled = client.post(f"/api/workflows/{wid}/toggle").json()
    assert toggled["enabled"] is False

    assert len(client.get(f"/api/workflows/{wid}/runs").json()) >= 1
    assert client.delete(f"/api/workflows/{wid}").status_code == 200


def test_workflow_creates_review_item_and_dismiss(client):
    client.post("/api/notes", json={"title": "Daily Summary", "content_md": "today"})
    # A create_review_item action linking to the entry.
    wf = client.post("/api/workflows", json={
        "name": "Daily review", "trigger_type": "event", "trigger_config": {"event": "noop"},
        "action_type": "create_review_item",
        "action_config": {"title": "Review your day", "message": "Summary ready", "link_title": "Daily Summary"},
        "enabled": True,
    }).json()
    client.post(f"/api/workflows/{wf['id']}/run")

    assert client.get("/api/reviews/count").json()["pending"] == 1
    items = client.get("/api/reviews").json()
    assert items[0]["title"] == "Review your day"
    assert items[0]["link_slug"] == "daily-summary"

    client.post(f"/api/reviews/{items[0]['id']}/dismiss")
    assert client.get("/api/reviews/count").json()["pending"] == 0


def test_append_action_with_review_block(client):
    wf = client.post("/api/workflows", json={
        "name": "Append+review", "trigger_type": "event", "trigger_config": {"event": "noop"},
        "action_type": "append_to_note",
        "action_config": {"title": "Journal", "text": "entry", "review": {"title": "Check journal"}},
        "enabled": True,
    }).json()
    client.post(f"/api/workflows/{wf['id']}/run")
    items = client.get("/api/reviews").json()
    assert any(i["title"] == "Check journal" and i["link_slug"] == "journal" for i in items)
