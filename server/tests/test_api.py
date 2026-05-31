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


def test_info_exposes_version_and_cors(client):
    from app.version import APP_VERSION
    info = client.get("/api/auth/info").json()
    assert info["version"] == APP_VERSION and "brain_name" in info
    # CORS header present for a cross-origin caller (separately-hosted PWA).
    r = client.get("/api/auth/info", headers={"Origin": "https://example.github.io"})
    assert r.headers.get("access-control-allow-origin") in ("*", "https://example.github.io")


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


def test_day_log_summary_workflow(client):
    from app.db import get_conn
    from app.services import quicktasks
    conn = get_conn()
    quicktasks.append_log(conn, "Daily Log", "woke up", date="2026-05-30")
    quicktasks.append_log(conn, "Daily Log", "shipped a feature", date="2026-05-30")
    quicktasks.append_log(conn, "Daily Log", "new day begins", date="2026-05-31")
    conn.commit()

    wf = client.post("/api/workflows", json={
        "name": "Day log", "trigger_type": "event", "trigger_config": {"event": "log_appended"},
        "action_type": "summarize_day_log",
        "action_config": {"log_title": "Daily Log", "summary_title": "Daily Summaries",
                          "review": {"title": "Daily review"}},
        "enabled": True,
    }).json()

    assert client.post(f"/api/workflows/{wf['id']}/run").json()["status"] == "ok"
    summ = client.get("/api/notes/daily-summaries").json()["content_md"]
    assert "## 2026-05-30" in summ and "woke up" in summ   # completed day summarised
    assert "2026-05-31" not in summ                         # current day left alone
    assert any("Daily review" in i["title"] for i in client.get("/api/reviews").json())

    # Idempotent: re-running doesn't re-summarise the same day.
    client.post(f"/api/workflows/{wf['id']}/run")
    assert client.get("/api/notes/daily-summaries").json()["content_md"].count("## 2026-05-30") == 1


def test_wiki_synthesis_workflow(client, monkeypatch):
    from app.db import get_conn
    from app.services import workflows as wf_svc

    # Two raw entries to synthesize.
    client.post("/api/notes", json={"title": "Ran 5k", "content_md": "felt great, 26 min"})
    client.post("/api/notes", json={"title": "Read on habits", "content_md": "tiny habits compound"})

    # Stub the Claude call with a deterministic KB action.
    monkeypatch.setattr(wf_svc, "_synthesize_actions", lambda entries, kb: [
        {"op": "create", "title": "Health & Habits",
         "content_md": "Synthesis. See [[Ran 5k]] and [[Read on habits]]."}
    ])

    wf = client.post("/api/workflows", json={
        "name": "Synth", "trigger_type": "schedule", "trigger_config": {"interval_seconds": 86400},
        "action_type": "synthesize_wiki", "action_config": {"review": {"title": "KB updated"}},
        "enabled": True,
    }).json()
    assert client.post(f"/api/workflows/{wf['id']}/run").json()["status"] == "ok"

    kb = client.get("/api/notes?kind=kb").json()
    assert any(n["title"] == "Health & Habits" for n in kb)
    note = client.get("/api/notes/health-habits").json()
    assert note["kind"] == "kb"
    # It links to the source entries -> they gain backlinks.
    assert any(b["title"] == "Health & Habits" for b in client.get("/api/notes/ran-5k").json()["backlinks"])
    assert any("KB updated" in i["title"] for i in client.get("/api/reviews").json())

    # Re-run with no new entries -> no-op (watermark advanced).
    assert "no new entries" in client.post(f"/api/workflows/{wf['id']}/run").json()["detail"]


def test_manual_edit_preserves_kb_kind(client):
    from app.db import get_conn
    from app.services import notes as notes_svc
    conn = get_conn()
    notes_svc.upsert_note(conn, "KB Topic", "v1", kind="kb")
    conn.commit()
    # Manual edit through the normal notes endpoint (no kind passed).
    client.post("/api/notes", json={"title": "KB Topic", "content_md": "edited"})
    note = client.get("/api/notes/kb-topic").json()
    assert note["kind"] == "kb" and note["content_md"] == "edited"


def test_system_version_check(client, monkeypatch):
    from app.routers import system
    from app.version import APP_VERSION
    # No newer release -> not available.
    monkeypatch.setattr(system, "_latest_release", lambda: {"tag": APP_VERSION, "url": "u", "name": "n"})
    v = client.get("/api/system/version").json()
    assert v["current"] == APP_VERSION and v["update_available"] is False
    # A newer release -> available.
    monkeypatch.setattr(system, "_latest_release", lambda: {"tag": "v999.0.0", "url": "u", "name": "n"})
    assert client.get("/api/system/version").json()["update_available"] is True


def test_system_version_tracks_main_commit(client, monkeypatch):
    from app.routers import system
    monkeypatch.setattr(system, "_latest_release", lambda: None)  # no tags/releases
    monkeypatch.setattr(system, "_latest_main_commit", lambda: {"sha": "abcdef1234567890", "url": "u"})
    monkeypatch.setenv("JBRAIN_BUILD_REF", "0000000deadbeef")
    monkeypatch.setattr(system, "_main_is_ahead", lambda ref: True)
    v = client.get("/api/system/version").json()
    assert v["update_available"] is True and v["latest"] == "main@abcdef1"
    # Up to date with main -> no banner.
    monkeypatch.setattr(system, "_main_is_ahead", lambda ref: False)
    assert client.get("/api/system/version").json()["update_available"] is False


def test_system_update_schedules_when_no_cmd(client, monkeypatch):
    import os
    monkeypatch.delenv("JBRAIN_UPDATE_CMD", raising=False)
    r = client.post("/api/system/update").json()
    assert r["scheduled"] is True
    from app.config import get_settings
    flag = os.path.join(os.path.dirname(get_settings().db_path), "update-requested.json")
    assert os.path.exists(flag)


def test_backup_and_restore(client):
    client.post("/api/notes", json={"title": "Keep Me", "content_md": "precious"})
    blob = client.get("/api/system/backup")
    assert blob.status_code == 200
    assert blob.content[:16] == b"SQLite format 3\x00"
    snapshot = blob.content

    # Mutate after the snapshot, then restore it.
    client.delete("/api/notes/keep-me")
    assert client.get("/api/notes/keep-me").status_code == 404

    r = client.post("/api/system/restore",
                    files={"file": ("backup.db", snapshot, "application/octet-stream")})
    assert r.status_code == 200
    assert client.get("/api/notes/keep-me").json()["title"] == "Keep Me"


def test_restore_rejects_non_sqlite(client):
    bad = client.post("/api/system/restore",
                      files={"file": ("x.db", b"not a database", "application/octet-stream")})
    assert bad.status_code == 400


def test_workflow_sync_and_reset(client, monkeypatch, tmp_path):
    from app.db import get_conn
    from app.services import workflows as wf_svc
    _write_workflow(str(tmp_path), "synced.yaml", """
key: synced-wf
name: Synced
enabled: true
trigger: { type: event, event: noop }
action: { type: append_to_note, config: { title: T, text: x } }
""")
    monkeypatch.setenv("JBRAIN_WORKFLOWS_DIR", str(tmp_path))

    out = client.post("/api/workflows/sync").json()
    assert out["synced"] == 1
    wf = next(w for w in client.get("/api/workflows").json() if w["key"] == "synced-wf")
    assert wf["locked"] is False

    # Editing locks it; reset unlocks (so repo can refresh it again).
    client.put(f"/api/workflows/{wf['id']}", json={
        "name": "Edited", "trigger_type": "event", "trigger_config": {"event": "noop"},
        "action_type": "append_to_note", "action_config": {"title": "T", "text": "x"}, "enabled": True,
    })
    assert client.get(f"/api/workflows/{wf['id']}").json()["locked"] is True
    assert client.post(f"/api/workflows/{wf['id']}/reset").json()["locked"] is False


def test_capture_with_location(client):
    client.post("/api/capture", json={"content": "at the park", "lat": 40.0, "lon": -73.0})
    item = next(i for i in client.get("/api/capture").json() if i["content"] == "at the park")
    assert item["lat"] == 40.0 and item["lon"] == -73.0


def test_staging_apply_stamps_location(client):
    import json as _json
    from app.db import get_conn
    conn = get_conn()
    conn.execute("INSERT INTO conversations (id, title) VALUES (77, 't')")
    conn.execute(
        "INSERT INTO messages (conversation_id, role, content, lat, lon) "
        "VALUES (77, 'user', 'hi', 12.34, 56.78)"
    )
    conn.execute(
        "INSERT INTO staging_actions (conversation_id, type, payload_json) VALUES (77, 'CREATE', ?)",
        (_json.dumps({"type": "CREATE", "title": "Placed Note", "content": "x", "summary": "s"}),),
    )
    conn.commit()
    pending = client.get("/api/staging").json()
    client.post(f"/api/staging/{pending[0]['id']}/apply")
    note = client.get("/api/notes/placed-note").json()
    assert note["lat"] == 12.34 and note["lon"] == 56.78


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
