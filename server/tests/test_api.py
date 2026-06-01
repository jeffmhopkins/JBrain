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


def test_info_public_and_version_is_authed(client):
    from app.version import APP_VERSION
    info = client.get("/api/auth/info").json()
    assert "brain_name" in info and "version" not in info   # version is not leaked pre-auth
    assert client.get("/api/auth/verify").json()["version"] == APP_VERSION
    # CORS header present for a cross-origin caller (separately-hosted PWA).
    r = client.get("/api/auth/info", headers={"Origin": "https://example.github.io"})
    assert r.headers.get("access-control-allow-origin") in ("*", "https://example.github.io")


def test_entry_mode_creates_unique_notes(client):
    # "Make entry": direct store, no LLM. Same title -> distinct notes (no merge).
    a = client.post("/api/notes/entry", json={"text": "first thought", "title": "Idea"}).json()
    b = client.post("/api/notes/entry", json={"text": "second thought", "title": "Idea"}).json()
    assert a["slug"] != b["slug"]
    assert client.get(f"/api/notes/{a['slug']}").json()["content_md"] == "first thought"
    # No title -> derived from first line; entries live under the notes/ root.
    c = client.post("/api/notes/entry", json={"text": "buy a tent\nfor camping"}).json()
    assert c["title"].startswith("notes/buy a tent")


def test_research_mode_is_read_only(client):
    # Research mode must not expose write tools.
    from app.services import architect
    research = {t.name for t in architect._tools_for("research")}
    assisted = {t.name for t in architect._tools_for("assisted")}
    assert "propose_actions" not in research and "add_list_item" not in research
    assert "query_sql" in research and "query_sql" not in assisted
    assert "propose_actions" in assisted


def test_query_sql_guard():
    from app.db import get_conn
    from app.services import sqlsafe
    cols, rows = sqlsafe.run_select(get_conn(), "SELECT 1 AS one", 10)
    assert cols == ["one"] and rows == [[1]]
    import pytest as _pytest
    with _pytest.raises(ValueError):
        sqlsafe.run_select(get_conn(), "DELETE FROM notes", 10)
    # The meta table (holds the access-key hash), recursive CTEs, and file
    # functions are all rejected.
    for bad in ("SELECT value FROM meta",
                "WITH RECURSIVE c(x) AS (SELECT 1) SELECT * FROM c",
                "SELECT readfile('/data/access-key.txt')"):
        with _pytest.raises(ValueError):
            sqlsafe.run_select(get_conn(), bad, 10)


def test_agent_config_complete_and_valid(client):
    from app.services import architect, prompts
    # The shipped prompts.yaml is the full agent config.
    assert prompts.get("modes.assisted.system") and prompts.get("modes.research.system")
    assert prompts.get_list("modes.assisted.tools") and prompts.get_list("modes.research.tools")
    assert prompts.get_int("agent.max_iterations", 0) > 0
    for t in architect._TOOL_SCHEMAS:
        assert prompts.get(f"tools.{t}"), f"missing description for tool {t}"
    for a in ("daylog_summary", "generate_tags", "synthesize", "wiki_synthesis"):
        assert prompts.get(f"actions.{a}")
    # No drift: tools referenced exist + are available where mentioned.
    from app.db import get_conn
    assert architect.validate_agent_config(get_conn()) == []


def test_tool_descriptions_come_from_yaml():
    from app.services import architect
    tools = {t.name: t.description for t in architect._tools_for("assisted")}
    assert "checklist" in tools["add_list_item"] and "shopping list" in tools["add_list_item"]


def test_research_prompt_injects_live_tables(client):
    from app.services import architect
    from app.db import get_conn
    sysp = architect._system_prompt("Demo", "research", get_conn())
    assert "{tables}" not in sysp and "notes" in sysp  # placeholder replaced with real tables


def test_prompt_editor_override_and_reset(client):
    from app.services import prompts
    listing = client.get("/api/prompts").json()
    assert any(p["key"] == "actions.generate_tags" for p in listing)

    client.put("/api/prompts/actions.generate_tags", json={"value": "CUSTOM TAG PROMPT"})
    assert prompts.get("actions.generate_tags") == "CUSTOM TAG PROMPT"
    row = next(p for p in client.get("/api/prompts").json() if p["key"] == "actions.generate_tags")
    assert row["override"] == "CUSTOM TAG PROMPT" and row["effective"] == "CUSTOM TAG PROMPT"

    client.delete("/api/prompts/actions.generate_tags")
    assert next(p for p in client.get("/api/prompts").json()
                if p["key"] == "actions.generate_tags")["override"] is None


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
    tl = client.get("/api/notes/notes-fromai/versions").json()
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
    assert client.get("/api/notes/notes-staged").json()["title"] == "notes/Staged"


def test_staged_create_does_not_clobber_existing_note(client):
    # A staged CREATE whose title collides with an existing note must NOT
    # overwrite it — it gets a disambiguated title instead.
    import json as _json
    from app.db import get_conn
    client.post("/api/notes/entry", json={"text": "my savings plan", "title": "Finances"})
    conn = get_conn()
    conn.execute(
        "INSERT INTO staging_actions (type, payload_json) VALUES ('CREATE', ?)",
        (_json.dumps({"type": "CREATE", "title": "Finances", "content": "REPLACED", "summary": "s"}),),
    )
    conn.commit()
    aid = client.get("/api/staging").json()[0]["id"]
    assert client.post(f"/api/staging/{aid}/apply").status_code == 200
    # Original note is untouched; a second, distinctly-titled note was created.
    assert client.get("/api/notes/notes-finances").json()["content_md"] == "my savings plan"
    titles = [n["title"] for n in client.get("/api/notes").json()]
    assert "notes/Finances" in titles and "notes/Finances (2)" in titles


def test_graph_nodes_include_kind(client):
    from app.db import get_conn
    from app.services import notes as notes_svc
    conn = get_conn()
    notes_svc.upsert_note(conn, "An Article", "kb body", kind="kb")
    conn.commit()
    client.post("/api/notes/entry", json={"text": "a raw entry", "title": "An Entry"})
    nodes = {n["title"]: n["kind"] for n in client.get("/api/graph").json()["nodes"]}
    assert nodes["An Article"] == "kb" and nodes["notes/An Entry"] == "entry"


def test_update_note_renames_in_place(client):
    # PUT renames a note (and its slug) in place instead of creating a duplicate;
    # backlinks (resolved by id) survive the rename.
    client.post("/api/notes/entry", json={"text": "body", "title": "Jeff"})
    client.post("/api/notes", json={"title": "Friend", "content_md": "see [[notes/Jeff]]"})
    r = client.put("/api/notes/notes-jeff", json={"title": "kb/Jeff", "content_md": "body"}).json()
    assert r["slug"] == "kb-jeff" and r["title"] == "kb/Jeff"
    # The old slug is gone; only one note exists (renamed, not duplicated).
    assert client.get("/api/notes/notes-jeff").status_code == 404
    titles = [n["title"] for n in client.get("/api/notes").json()]
    assert "kb/Jeff" in titles and "notes/Jeff" not in titles
    # Inbound [[notes/Jeff]] references were rewritten so links don't dangle.
    assert "[[kb/Jeff]]" in client.get("/api/notes/friend").json()["content_md"]
    assert any(b["title"] == "Friend" for b in client.get("/api/notes/kb-jeff").json()["backlinks"])
    # Renaming onto an existing title is rejected.
    client.post("/api/notes/entry", json={"text": "x", "title": "Taken"})
    assert client.put("/api/notes/kb-jeff", json={"title": "notes/Taken", "content_md": "body"}).status_code == 409


def test_apply_records_event_message_in_conversation(client):
    # Applying a staged action leaves a persistent 'event' record in the chat
    # (so approvals stay in the conversation across reloads).
    import json as _json
    from app.db import get_conn
    conn = get_conn()
    conn.execute("INSERT INTO conversations (id, title) VALUES (88, 'c')")
    conn.execute(
        "INSERT INTO staging_actions (conversation_id, type, payload_json) VALUES (88, 'CREATE', ?)",
        (_json.dumps({"type": "CREATE", "title": "Recorded", "content": "x", "summary": "s"}),),
    )
    conn.commit()
    aid = client.get("/api/staging").json()[0]["id"]
    assert client.post(f"/api/staging/{aid}/apply").status_code == 200
    msgs = client.get("/api/chat/conversations/88/messages").json()
    events = [m for m in msgs if m["role"] == "event"]
    assert events and "Recorded" in events[0]["content"]


def test_staged_action_missing_title_is_400_not_500(client):
    import json as _json
    from app.db import get_conn
    conn = get_conn()
    conn.execute(
        "INSERT INTO staging_actions (type, payload_json) VALUES ('CREATE', ?)",
        (_json.dumps({"type": "CREATE", "summary": "no title here"}),),
    )
    conn.commit()
    aid = client.get("/api/staging").json()[0]["id"]
    assert client.post(f"/api/staging/{aid}/apply").status_code == 400
    # The failed apply rolled back -> the row is still pending (retryable), not lost.
    assert client.get("/api/staging").json()[0]["status"] == "pending"


def test_apply_action_is_not_double_applied(client):
    import json as _json
    from app.db import get_conn
    conn = get_conn()
    conn.execute(
        "INSERT INTO staging_actions (type, payload_json) VALUES ('CREATE', ?)",
        (_json.dumps({"type": "CREATE", "title": "Once", "content": "x", "summary": "s"}),),
    )
    conn.commit()
    aid = client.get("/api/staging").json()[0]["id"]
    assert client.post(f"/api/staging/{aid}/apply").status_code == 200
    # Re-applying the same (now non-pending) row is rejected, not silently redone.
    assert client.post(f"/api/staging/{aid}/apply").status_code == 404


def test_synthesis_lives_under_kb_root_separate_from_entry(client, monkeypatch):
    # Entries (notes/) and the synthesized article (kb/) occupy separate title
    # roots, so the same topic never collides and the entry is untouched.
    from app.services import workflows as wf_svc
    client.post("/api/notes/entry", json={"text": "user-authored body", "title": "Project Atlas"})
    monkeypatch.setattr(wf_svc, "_synthesize_actions", lambda entries, kb, instructions=None, **_: [
        {"title": "Project Atlas", "content_md": "ENCYCLOPEDIA VERSION"}
    ])
    wf = client.post("/api/workflows", json={
        "name": "Synth", "trigger_type": "schedule", "trigger_config": {"interval_seconds": 86400},
        "action_type": "synthesize_wiki", "action_config": {}, "enabled": True,
    }).json()
    assert client.post(f"/api/workflows/{wf['id']}/run").json()["status"] == "ok"
    atlas = client.get("/api/notes/notes-project-atlas").json()
    assert atlas["content_md"] == "user-authored body" and atlas["kind"] == "entry"  # untouched
    # The KB article takes the clean topic name under kb/ (no "(2)").
    assert any(n["title"] == "kb/Project Atlas" and n["kind"] == "kb"
               for n in client.get("/api/notes?kind=kb").json())


def test_empty_entry_rejected(client):
    assert client.post("/api/notes/entry", json={"text": "   "}).status_code == 422


def test_out_of_range_coords_rejected(client):
    r = client.post("/api/notes/entry", json={"text": "here", "lat": 999, "lon": 0})
    assert r.status_code == 422


def test_research_mode_blocks_write_tools():
    # The mode boundary is enforced in _run_tool, not just by tool advertisement.
    from app.services import architect
    msg, event = architect._run_tool(None, None, "propose_actions", {"actions": []}, mode="research")
    assert "not available" in msg and event is None


def test_untrusted_fence_uses_a_nonce():
    from app.services import architect
    a = architect._untrusted("note", "body")
    b = architect._untrusted("note", "body")
    # Random per-call delimiter => a crafted body can't predict/close the fence.
    assert a != b and "body" in a


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


def test_attachments_accepts_any_file(client):
    client.post("/api/notes", json={"title": "Host2", "content_md": "x"})
    # Any file type is accepted now (binary stored; no text extracted from junk).
    ok = client.post(
        "/api/notes/host2/attachments",
        files={"file": ("image.png", b"\x89PNG\r\nnotrealpng", "image/png")},
    )
    assert ok.status_code == 200
    assert any(a["filename"] == "image.png" for a in client.get("/api/notes/host2/attachments").json())


def test_attachments_rejects_oversize(client):
    client.post("/api/notes", json={"title": "Host3", "content_md": "x"})
    big = client.post(
        "/api/notes/host3/attachments",
        files={"file": ("big.bin", b"x" * (10 * 1024 * 1024 + 1), "application/octet-stream")},
    )
    assert big.status_code == 413


def test_attachment_text_is_extracted_and_downloads(client):
    client.post("/api/notes", json={"title": "Host4", "content_md": "x"})
    up = client.post(
        "/api/notes/host4/attachments",
        files={"file": ("notes.txt", b"searchable plain text here", "text/plain")},
    ).json()
    got = client.get(f"/api/attachments/{up['id']}").json()
    assert "searchable plain text" in got["content_text"]
    dl = client.get(f"/api/attachments/{up['id']}/download")
    assert dl.content == b"searchable plain text here"


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


def test_schedule_due_cron_and_interval():
    from datetime import datetime, timedelta, timezone
    from app.services import workflows as wf_svc
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)

    # Interval: never-run is due; just-run is not.
    assert wf_svc.schedule_due(None, {"interval_seconds": 3600}, now) is True
    assert wf_svc.schedule_due("2026-05-31 11:59:00", {"interval_seconds": 3600}, now) is False

    # Cron (every minute): never-run does NOT fire immediately; an old last-run does.
    if __import__("importlib").util.find_spec("croniter"):
        assert wf_svc.schedule_due(None, {"cron": "* * * * *"}, now) is False
        assert wf_svc.schedule_due("2026-05-31 11:55:00", {"cron": "* * * * *"}, now) is True
        # Daily 07:00 — last run yesterday, now past 07:00 today -> due.
        assert wf_svc.schedule_due("2026-05-30 07:00:00", {"cron": "0 7 * * *"}, now) is True


def test_daylog_prompt_is_configurable(client, monkeypatch):
    from app.services import workflows as wf_svc
    captured = {}
    monkeypatch.setattr(wf_svc, "_summarise_entries",
                        lambda entries, prompt=None: captured.update(prompt=prompt) or "ok")
    from app.db import get_conn
    from app.services import quicktasks
    conn = get_conn()
    quicktasks.append_log(conn, "Daily Log", "a", date="2026-05-29")
    quicktasks.append_log(conn, "Daily Log", "b", date="2026-05-30")
    conn.commit()
    wf = client.post("/api/workflows", json={
        "name": "DL", "trigger_type": "event", "trigger_config": {"event": "x"},
        "action_type": "summarize_day_log",
        "action_config": {"log_title": "Daily Log", "prompt": "MY CUSTOM PROMPT"}, "enabled": True,
    }).json()
    client.post(f"/api/workflows/{wf['id']}/run")
    assert captured.get("prompt") == "MY CUSTOM PROMPT"


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
    monkeypatch.setattr(wf_svc, "_synthesize_actions", lambda entries, kb, instructions=None, **_: [
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
    assert any(n["title"] == "kb/Health & Habits" for n in kb)
    note = client.get("/api/notes/kb-health-habits").json()
    assert note["kind"] == "kb"
    # It links to the source entries -> they gain backlinks.
    assert any(b["title"] == "kb/Health & Habits" for b in client.get("/api/notes/ran-5k").json()["backlinks"])
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


def test_entry_created_triggers_autotag(client, monkeypatch):
    from app.services import workflows as wf_svc
    monkeypatch.setattr(wf_svc, "_suggest_tags", lambda title, content, prompt=None: ["work", "planning"])
    client.post("/api/workflows", json={
        "name": "Autotag", "trigger_type": "event", "trigger_config": {"event": "entry_created"},
        "action_type": "generate_tags", "action_config": {}, "enabled": True,
    })
    # Creating a NEW entry fires entry_created -> the workflow tags it.
    client.post("/api/notes", json={"title": "Project Kickoff", "content_md": "plan the launch"})
    tags = set(client.get("/api/notes/project-kickoff").json()["tags"])
    assert {"work", "planning"} <= tags


def test_entry_created_not_fired_for_kb(client, monkeypatch):
    # Synthesized kb notes (source=workflow, kind=kb) must NOT trigger entry_created.
    calls = {"n": 0}
    from app.services import workflows as wf_svc
    monkeypatch.setattr(wf_svc, "_suggest_tags", lambda *a, **k: calls.update(n=calls["n"] + 1) or ["x"])
    client.post("/api/workflows", json={
        "name": "Autotag", "trigger_type": "event", "trigger_config": {"event": "entry_created"},
        "action_type": "generate_tags", "action_config": {}, "enabled": True,
    })
    from app.db import get_conn
    from app.services import notes as notes_svc
    conn = get_conn()
    notes_svc.upsert_note(conn, "KB X", "body", kind="kb", source="workflow")
    conn.commit()
    assert calls["n"] == 0  # kb/workflow write did not fire the entry hook


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
    note = client.get("/api/notes/notes-placed-note").json()
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


# --- Declarative pipeline engine -------------------------------------------

def test_pipeline_templating_native_and_string():
    from app.services import pipeline
    scope = {"config": {"n": 3, "tags": ["a", "b"]}, "today": "2026-01-01"}
    # A lone {{ expr }} yields the native value (list/int), not a string.
    assert pipeline._render_value("{{ config.tags }}", scope) == ["a", "b"]
    assert pipeline._render_value("{{ config.n }}", scope) == 3
    # Embedded interpolation yields a string; {date} is substituted.
    assert pipeline._render_value("have {{ config.n }} on {date}", scope) == "have 3 on 2026-01-01".replace("2026-01-01", pipeline._today())
    # Missing keys chain to None/empty rather than raising.
    assert pipeline._render_value("{{ config.missing }}", scope) is None
    assert pipeline._render_value("{{ config.missing.deep }}", scope) is None


def test_pipeline_default_filter_and_concat():
    from app.services import pipeline
    scope = {"config": {"title": "Journal"}}
    assert pipeline._eval("config.review.title | default('Review: ' ~ config.title)", scope) == "Review: Journal"
    assert pipeline._eval("config.review", scope) is None  # tri-state: absent → falsy


def test_action_defs_load_and_validate():
    from app.services import pipeline
    types = pipeline.action_types()
    for t in ("append_to_note", "create_review_item", "generate_tags",
              "summarize_day_log", "synthesize_wiki"):
        assert t in types, f"{t} should be a YAML-defined action"
    assert pipeline.validate_action_defs() == []  # shipped defs are well-formed


def test_synthesize_wiki_watermark_not_advanced_on_empty_plan(client, monkeypatch):
    # Regression: a failed/empty LLM plan must NOT advance the watermark, else the
    # entry would be skipped forever. (The old Python action advanced it always.)
    from app.services import workflows as wf_svc
    client.post("/api/notes", json={"title": "Entry One", "content_md": "a"})

    wf = client.post("/api/workflows", json={
        "name": "S", "trigger_type": "schedule", "trigger_config": {"interval_seconds": 1},
        "action_type": "synthesize_wiki", "action_config": {}, "enabled": True,
    }).json()

    # Run 1: LLM yields nothing → no KB note, watermark stays put.
    monkeypatch.setattr(wf_svc, "_synthesize_actions", lambda entries, kb, instructions=None, **_: [])
    client.post(f"/api/workflows/{wf['id']}/run")
    assert client.get("/api/notes?kind=kb").json() == []

    # Run 2: LLM works → the SAME entry is still processed (not skipped).
    monkeypatch.setattr(wf_svc, "_synthesize_actions", lambda entries, kb, instructions=None, **_: [
        {"op": "create", "title": "Topic", "content_md": "from [[Entry One]]"}])
    client.post(f"/api/workflows/{wf['id']}/run")
    assert any(n["title"] == "kb/Topic" for n in client.get("/api/notes?kind=kb").json())


def test_pipeline_unknown_primitive_raises():
    from app.db import get_conn
    from app.services import pipeline
    bad = {"type": "x", "steps": [{"do": "nonexistent", "with": {}}]}
    try:
        pipeline.run_pipeline(get_conn(), bad, {}, 1, None)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "unknown primitive" in str(e)


def test_pipeline_action_runs_via_engine(client):
    # append_to_note is a YAML definition now; confirm dispatch routes to it.
    from app.services import pipeline
    assert pipeline.get_action_def("append_to_note") is not None
    wf = client.post("/api/workflows", json={
        "name": "Eng", "trigger_type": "event", "trigger_config": {"event": "noop"},
        "action_type": "append_to_note",
        "action_config": {"title": "Engine Out", "text": "x"}, "enabled": True,
    }).json()
    assert client.post(f"/api/workflows/{wf['id']}/run").json()["status"] == "ok"
    assert "x" in client.get("/api/notes/engine-out").json()["content_md"]


def test_action_types_endpoint(client):
    cat = client.get("/api/workflows/action-types").json()
    by_type = {c["type"]: c for c in cat}
    # Every runnable action appears under its canonical (agnostic) name; the
    # legacy alias `claude_synthesize` is NOT surfaced in the picker.
    for t in ("append_to_note", "create_review_item", "generate_tags",
              "summarize_day_log", "synthesize_wiki", "synthesize"):
        assert t in by_type, f"{t} missing from action catalog"
    assert "claude_synthesize" not in by_type
    # Schemas come through for the picker/forms.
    assert any(f["key"] == "title" for f in by_type["append_to_note"]["config"])
    assert any(f["key"] == "target_title" for f in by_type["synthesize"]["config"])


def test_claude_synthesize_via_pipeline(client, monkeypatch):
    # Uses the LEGACY action_type 'claude_synthesize' on purpose: proves an
    # existing DB row still dispatches to the renamed 'synthesize' recipe via the
    # alias. Mock the llm primitive (no API key in CI).
    from app.services import pipeline
    client.post("/api/notes", json={"title": "Src", "content_md": "raw material"})
    monkeypatch.setitem(pipeline._PRIMITIVES, "llm", lambda ctx, **k: "SYNTHESISED")

    wf = client.post("/api/workflows", json={
        "name": "CS", "trigger_type": "event", "trigger_config": {"event": "noop"},
        "action_type": "claude_synthesize",
        "action_config": {"target_title": "Summary", "source_title": "Src",
                          "review": {"title": "Check synthesis"}},
        "enabled": True,
    }).json()
    assert client.post(f"/api/workflows/{wf['id']}/run").json()["status"] == "ok"
    assert client.get("/api/notes/summary").json()["content_md"] == "SYNTHESISED"
    assert any(i["title"] == "Check synthesis" for i in client.get("/api/reviews").json())


# --- Provider-agnostic LLM layer -------------------------------------------

def test_llm_settings_env_aliases(monkeypatch):
    from app.config import Settings
    # Canonical LLM_* names populate the fields.
    monkeypatch.setenv("LLM_API_KEY", "newkey")
    monkeypatch.setenv("LLM_MODEL", "model-x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    s = Settings(_env_file=None)
    assert s.llm_api_key == "newkey" and s.llm_model == "model-x"
    assert s.has_llm and s.has_anthropic                       # alias property
    assert s.anthropic_api_key == "newkey" and s.anthropic_model == "model-x"

    # Legacy ANTHROPIC_* names still work (back-compat for existing .env files).
    monkeypatch.delenv("LLM_API_KEY")
    monkeypatch.delenv("LLM_MODEL")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "oldkey")
    monkeypatch.setenv("ANTHROPIC_MODEL", "old-model")
    s2 = Settings(_env_file=None)
    assert s2.llm_api_key == "oldkey" and s2.llm_model == "old-model" and s2.has_llm


def test_llm_provider_basics():
    from app.services import llm
    p = llm.get_provider()
    assert p.name == "anthropic" and p.supports_tools() is True
    assert isinstance(p.default_model(), str) and p.default_model()
    # Tool defs are neutral dataclasses now.
    from app.services import architect
    td = architect._tools_for("assisted")[0]
    assert isinstance(td, llm.ToolDef) and td.name and isinstance(td.json_schema, dict)


def test_prompt_key_alias_preserves_legacy_override(client):
    # A customisation saved under the old key (actions.claude_synthesize) must
    # still apply after the rename to actions.synthesize.
    from app.services import prompts
    from app.db import get_conn
    conn = get_conn()
    prompts.set_override(conn, "actions.claude_synthesize", "LEGACY CUSTOM")
    conn.commit()
    assert prompts.get("actions.synthesize") == "LEGACY CUSTOM"
    # A new-key override takes precedence over the legacy one.
    prompts.set_override(conn, "actions.synthesize", "NEW CUSTOM")
    conn.commit()
    assert prompts.get("actions.synthesize") == "NEW CUSTOM"


def test_action_def_db_first_and_custom_type(client):
    # Recipes resolve from the action_defs table (seeded at boot), custom types
    # appear in the catalog, and edits are reflected (updated_at-keyed cache).
    import yaml
    from app.db import get_conn
    from app.services import pipeline
    conn = get_conn()
    assert "synthesize" in pipeline.action_types()
    assert pipeline.get_action_def("synthesize") is not None
    # legacy alias still resolves to the canonical recipe via the file alias map
    assert pipeline.get_action_def("claude_synthesize")["type"] == "synthesize"

    recipe = {"type": "say_hi", "steps": [{"do": "create_review", "with": {"title": "hi"}}]}
    conn.execute("INSERT INTO action_defs (type, recipe_yaml, source, locked) VALUES (?,?,'user',1)",
                 ("say_hi", yaml.safe_dump(recipe)))
    conn.commit()
    assert "say_hi" in pipeline.action_types()
    assert pipeline.get_action_def("say_hi")["type"] == "say_hi"

    recipe["steps"][0]["with"]["title"] = "bye"
    conn.execute("UPDATE action_defs SET recipe_yaml=?, updated_at=datetime('now','+2 seconds') "
                 "WHERE type='say_hi'", (yaml.safe_dump(recipe),))
    conn.commit()
    assert pipeline.get_action_def("say_hi")["steps"][0]["with"]["title"] == "bye"


def test_primitive_meta_pinned_to_primitives():
    # CI guard: metadata can't drift from the actual primitive functions.
    import inspect
    from app.services import pipeline
    assert set(pipeline._PRIMITIVE_META) == set(pipeline._PRIMITIVES)
    for name, meta in pipeline._PRIMITIVE_META.items():
        params = set(inspect.signature(pipeline._PRIMITIVES[name]).parameters)
        for inp in meta["inputs"]:
            assert inp["name"] in params, f"{name}: input {inp['name']} not a real param"


def test_action_defs_api(client):
    client.post("/api/action-defs/sync")  # seed the table from repo (lifespan does this in prod)
    defs = {d["type"]: d for d in client.get("/api/action-defs").json()}
    assert defs["synthesize"]["source"] == "repo" and defs["synthesize"]["num_steps"] >= 3
    assert any(p["name"] == "write_note" for p in client.get("/api/action-defs/primitives").json())

    got = client.get("/api/action-defs/synthesize").json()
    assert got["recipe"]["type"] == "synthesize" and got["warnings"] == []
    # Shipped recipes are read-only.
    assert client.put("/api/action-defs/synthesize", json={"recipe_yaml": got["recipe_yaml"]}).status_code == 403

    # Validate surfaces lint warnings without saving.
    bad = "type: x\nsteps:\n  - do: write_note\n    with: {title: '{{ missing }}'}\n"
    assert any("missing" in w for w in client.post("/api/action-defs/validate", json={"recipe_yaml": bad}).json()["warnings"])

    # Create a custom action -> it appears in the Workflows picker (regression guard).
    recipe = "type: my_custom\nsteps:\n  - do: create_review\n    with: {title: hi}\n"
    assert client.post("/api/action-defs", json={"recipe_yaml": recipe}).json()["type"] == "my_custom"
    assert client.post("/api/action-defs", json={"recipe_yaml": recipe}).status_code == 409  # duplicate
    assert "my_custom" in {a["type"] for a in client.get("/api/workflows/action-types").json()}
    assert client.delete("/api/action-defs/my_custom").status_code == 200


def test_call_action_chaining_cycle_and_returns(client):
    import yaml
    import pytest as _pytest
    from app.db import get_conn
    from app.services import pipeline
    conn = get_conn()

    def put(recipe):
        conn.execute("INSERT INTO action_defs (type, recipe_yaml, source, locked) VALUES (?,?,'user',1)",
                     (recipe["type"], yaml.safe_dump(recipe)))
    put({"type": "inner_act", "steps": [{"do": "create_review", "with": {"title": "from inner"}}]})
    put({"type": "outer_act", "steps": [{"do": "call_action", "with": {"action": "inner_act"}}]})
    put({"type": "cyc", "steps": [{"do": "call_action", "with": {"action": "cyc"}}]})
    put({"type": "greet", "returns": "{{ config.who }}",
         "steps": [{"do": "create_review", "with": {"title": "hi"}}]})
    conn.commit()

    # Composition: outer runs inner, whose review is created.
    pipeline.run_pipeline(conn, pipeline.get_action_def("outer_act"), {}, None, None)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM review_items WHERE title='from inner'").fetchone()["c"] == 1

    # Cycle guard: a self-referential recipe raises (bounded, never hangs).
    with _pytest.raises(RuntimeError):
        pipeline.run_pipeline(conn, pipeline.get_action_def("cyc"), {}, None, None)

    # returns: channel hands a value back to the caller.
    out = pipeline._p_call_action(pipeline._Ctx(conn, None, None), "greet", config={"who": "world"})
    assert out["return"] == "world" and out["type"] == "greet"


def test_wiki_entry_block_is_citeable():
    from app.services import workflows as wf
    b = wf._entry_block({"title": "Daily Log", "content_md": "woke up", "created_at": "2026-05-30 09:00:00"})
    assert b.startswith("## Daily Log\n")           # heading is the exact title (link resolves)
    assert "Cite this entry as [[Daily Log]]." in b and "Logged 2026-05-30" in b


def test_wiki_relevant_kb_retrieval(client, monkeypatch):
    # Retrieval returns the semantically-matched KB articles (filtered to kind=kb);
    # falls back to the passed list at cold start (no conn).
    from app.db import get_conn
    from app.services import workflows as wf, embeddings, notes as notes_svc
    conn = get_conn()
    notes_svc.upsert_note(conn, "Marathon Training", "training", kind="kb")
    notes_svc.upsert_note(conn, "An Entry", "raw note", kind="entry")  # not kb -> must be filtered out
    conn.commit()
    mid = notes_svc.get_by_title(conn, "Marathon Training")["id"]
    eid = notes_svc.get_by_title(conn, "An Entry")["id"]
    monkeypatch.setattr(embeddings, "semantic_search",
                        lambda c, q, limit=8: [{"id": mid, "title": "Marathon Training", "slug": "x", "distance": 0.1},
                                               {"id": eid, "title": "An Entry", "slug": "y", "distance": 0.2}])
    entries = [{"id": 99, "title": "Tempo run", "content_md": "ran fast"}]
    kb = wf._relevant_kb(conn, entries, fallback_kb=[{"title": "Cooking", "content_md": "x"}])
    assert [k["title"] for k in kb] == ["Marathon Training"]            # retrieved + kb-filtered
    fb = [{"title": "Cooking", "content_md": "x"}]
    assert wf._relevant_kb(None, entries, fallback_kb=fb) == fb         # cold start -> fallback


def test_wiki_synthesis_recovers_truncated_array(monkeypatch):
    # A reply truncated mid-array must NOT drop the whole batch: we salvage the
    # complete objects parsed before the truncation point.
    from app.services import workflows as wf, llm
    monkeypatch.setattr(llm, "has_credentials", lambda: True)
    monkeypatch.setattr(wf, "_relevant_kb", lambda *a, **k: [])
    monkeypatch.setattr(
        llm, "complete",
        lambda *a, **k: 'Here you go: [{"title":"A","content_md":"ok"}, {"title":"B","content_md":"trunc')
    out = wf._synthesize_actions([{"title": "e", "content_md": "x"}], [], conn=None)
    assert [a["title"] for a in out] == ["A"]   # the one complete article survives


def test_agent_loop_signals_when_it_stops_early(client, monkeypatch):
    # When the model keeps wanting tools until the iteration cap, the reply must
    # carry a visible "stopped early" notice rather than being silently cut off.
    import asyncio
    from app.services import architect, llm

    class _FakeProvider:
        name = "fake"
        def has_credentials(self): return True
        def default_model(self): return "m"
        def supports_tools(self): return True
        async def stream_turn(self, messages, *, system, tools, model, max_tokens):
            yield llm.TextDelta("thinking ")
            call = llm.ToolCall(id="t1", name="list_recent_notes", args={})
            yield llm.ToolCallEvent(call)
            yield llm.TurnEnd([call], usage={"input_tokens": 1, "output_tokens": 1})
        def append_tool_results(self, messages, results):
            messages.append({"role": "user", "content": "tool results"})

    monkeypatch.setattr(llm, "get_provider", lambda: _FakeProvider())
    conv_id = client.post("/api/chat/conversations").json()["id"]

    async def drain():
        return [ev async for ev in architect.run(conv_id, "hello")]

    events = asyncio.run(drain())
    text = "".join(e.get("text", "") for e in events if e.get("type") == "token")
    assert "stopped here" in text.lower() or "limit" in text.lower()
    assert events[-1] == {"type": "done"}


def test_parse_json_array_handles_brackets_in_strings():
    from app.services.workflows import _parse_json_array
    # Square brackets inside content_md (wiki-links) and prose around the array
    # must not confuse the extractor.
    txt = 'sure:\n[{"title":"T","content_md":"see [[Other]] and [list]"}]\nthanks!'
    assert _parse_json_array(txt) == [{"title": "T", "content_md": "see [[Other]] and [list]"}]
    assert _parse_json_array("nothing here") == []
    assert _parse_json_array("[]") == []


def test_staged_update_conflict_is_rejected(client):
    # Optimistic concurrency: an UPDATE proposed against a note that then changes
    # must be refused at apply, not silently clobber the newer content.
    from app.db import get_conn
    from app.services import architect
    client.post("/api/notes/entry", json={"text": "v1 body", "title": "Doc"})
    conn = get_conn()
    architect._tool_propose_actions(conn, None, [
        {"type": "UPDATE", "title": "notes/Doc", "content": "model rewrite", "summary": "s"}])
    conn.commit()
    # Intervening edit changes the note's content -> the basis hash no longer matches.
    client.post("/api/notes", json={"title": "notes/Doc", "content_md": "v2 body (user edit)"})
    aid = client.get("/api/staging").json()[0]["id"]
    assert client.post(f"/api/staging/{aid}/apply").status_code == 409
    assert client.get("/api/notes/notes-doc").json()["content_md"] == "v2 body (user edit)"  # not clobbered


def test_non_ascii_titles_get_distinct_slugs(client):
    # Emoji/CJK titles still get distinct (non-colliding) slugs under notes/.
    a = client.post("/api/notes/entry", json={"text": "🎉"}).json()
    b = client.post("/api/notes/entry", json={"text": "日本語"}).json()
    assert a["slug"] != b["slug"]
    assert a["title"].startswith("notes/") and b["title"].startswith("notes/")


def test_query_sql_is_read_only_and_blocks_schema_and_secrets(client):
    # The SQL console / research query_sql runs on a read-only connection AND
    # the keyword filter blocks meta + the sqlite_* schema tables.
    assert client.post("/api/sql", json={"sql": "SELECT name FROM sqlite_master"}).status_code == 400
    assert client.post("/api/sql", json={"sql": "SELECT value FROM meta"}).status_code == 400
    # A write is rejected by the filter, and even a filter-bypassing write can't
    # mutate the DB: PRAGMA query_only=ON is set on the connection.
    from app.db import get_query_conn
    import sqlite3 as _sqlite
    try:
        get_query_conn().execute("INSERT INTO notes (title, slug, content_md) VALUES ('x','x','x')")
        raised = False
    except _sqlite.OperationalError:
        raised = True
    assert raised  # read-only connection refuses writes structurally


def test_research_schema_tables_excludes_secrets(client):
    from app.services import architect
    from app.db import get_conn
    tables = architect._schema_tables(get_conn())
    assert "notes" in tables
    assert "meta" not in tables and "prompt_overrides" not in tables and "staging_actions" not in tables


def test_list_recent_notes_is_fenced(client):
    # Titles are user-controlled; the tool output must be wrapped as untrusted.
    from app.db import get_conn
    from app.services import architect, notes as notes_svc
    conn = get_conn()
    notes_svc.upsert_note(conn, "A Title", "body")
    conn.commit()
    out = architect._tool_list_recent(conn)
    assert "untrusted content" in out and "A Title" in out


def test_basisless_update_does_not_clobber(client):
    # An UPDATE proposed for a title that didn't exist at propose time must NOT
    # overwrite a note later created under that title — it creates a new one.
    from app.db import get_conn
    from app.services import architect
    conn = get_conn()
    architect._tool_propose_actions(conn, None, [
        {"type": "UPDATE", "title": "notes/Later", "content": "model content", "summary": "s"}])
    conn.commit()
    client.post("/api/notes/entry", json={"text": "user body", "title": "Later"})
    aid = client.get("/api/staging").json()[0]["id"]
    assert client.post(f"/api/staging/{aid}/apply").status_code == 200
    assert client.get("/api/notes/notes-later").json()["content_md"] == "user body"  # untouched
    assert any(n["title"] == "notes/Later (2)" for n in client.get("/api/notes").json())


def test_id_targeted_upsert_does_not_flip_kind(client):
    # Writing by note_id must never convert a note's kind (the cross-kind guard
    # only covers the title path; the id path must refuse the flip).
    from app.db import get_conn
    from app.services import notes as notes_svc
    conn = get_conn()
    nid = notes_svc.upsert_note(conn, "Mine", "body", source="user")  # kind=entry
    conn.commit()
    notes_svc.upsert_note(conn, "Mine", "new body", note_id=nid, kind="kb")
    conn.commit()
    assert conn.execute("SELECT kind FROM notes WHERE id = ?", (nid,)).fetchone()["kind"] == "entry"


def test_undo_noop_does_not_mark_undone(client):
    # If the line to remove is already gone, undo must report a conflict rather
    # than lying to the UI by marking the action 'undone'.
    import json as _json
    from app.db import get_conn
    from app.services import quicktasks
    conn = get_conn()
    quicktasks.add_list_item(conn, "Tasks", "buy milk")
    cur = conn.execute(
        "INSERT INTO staging_actions (type, payload_json, status) VALUES ('ADD_ITEM', ?, 'applied')",
        (_json.dumps({"summary": "x", "undo": {"op": "remove_line", "title": "Tasks", "line": "- [ ] NOT PRESENT"}}),),
    )
    conn.commit()
    assert client.post(f"/api/staging/{cur.lastrowid}/undo").status_code == 409
    # Still 'applied' (truthful), not falsely 'undone'.
    row = conn.execute("SELECT status FROM staging_actions WHERE id = ?", (cur.lastrowid,)).fetchone()
    assert row["status"] == "applied"
