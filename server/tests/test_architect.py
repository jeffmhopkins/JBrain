"""Owner Architect agent — the tool-using chat loop (Entry/Research/Full-Brain).

These drive `architect.run()` end-to-end with a SCRIPTED, no-network LLM (the `llm.get_provider`
seam returns a FakeProvider whose async `stream_turn` replays canned text + tool calls), so we
exercise the real dispatch loop, individual tool handlers, the staged-write path, the quick
additive ops that auto-apply + record an Undo, and the step/token-limit (auto-continue) branch —
asserting OBSERVABLE outcomes (DB writes, staged payloads, refusals, the "ask me to continue"
notice), never implementation detail.

Net-new only: nothing under app/ is touched. The LLM is mocked at the `llm` seam exactly as
test_research_labs_ai.py mocks `get_provider`; the DB is a raw in-memory sqlite3 over app/schema.sql
(the test_architect_truth.py pattern). One integration mark — it needs a DB.
"""
import asyncio
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("anthropic")

from app.services import architect
from app.services import llm
from app.services.llm import TextDelta, ToolCall, ToolCallEvent, TurnEnd

pytestmark = pytest.mark.integration

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


# --- scripted, no-network provider ------------------------------------------

class FakeProvider:
    """Replays a script of model turns into architect.run()'s loop. Each script entry is
    (text, [ToolCall]); a turn with no tool calls ends the loop. `always` makes EVERY turn
    request a tool (to drive the iteration/auto-continue bound). Mirrors the real provider
    contract: stream_turn appends the assistant turn to `messages` and append_tool_results
    appends the tool answers, so the loop's message bookkeeping runs for real."""

    def __init__(self, script=None, always=None):
        self.script = list(script or [])
        self.always = always
        self.tools_seen = None
        self.tool_results = []
        self.turns = 0

    def has_credentials(self):
        return True

    def default_model(self):
        return "fake-model"

    def supports_tools(self):
        return True

    async def stream_turn(self, messages, *, system, tools, model, max_tokens, thinking=False):
        self.tools_seen = [t.name for t in tools]
        self.turns += 1
        if self.always is not None:
            text, calls = self.always
        elif self.script:
            text, calls = self.script.pop(0)
        else:
            text, calls = ("All done.", [])
        if text:
            yield TextDelta(text)
        messages.append({"role": "assistant", "content": text or None})
        for c in calls:
            yield ToolCallEvent(c)
        yield TurnEnd(calls, usage={"input_tokens": 10, "output_tokens": 5})

    def append_tool_results(self, messages, results):
        for r in results:
            self.tool_results.append(r.content)
            messages.append({"role": "user", "content": r.content})


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    c.commit()
    return c


def _conv(conn):
    conn.execute("INSERT INTO conversations DEFAULT VALUES")
    return conn.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _drive(conn, monkeypatch, provider, user_text, *, mode="assisted", cid=None):
    """Run architect.run() to completion with the conn + provider wired in, collecting every
    streamed event. Returns (events, conversation_id)."""
    from app.services import architect as arch
    from app.services import embeddings
    from app import db as db_mod
    monkeypatch.setattr(arch, "get_conn", lambda: conn)
    monkeypatch.setattr(db_mod, "get_query_conn", lambda: conn)
    monkeypatch.setattr(llm, "get_provider", lambda *a, **k: provider)
    # Stub the embedding seam: the write tools call notes.upsert_note, which would otherwise
    # hit the sqlite-vec virtual tables (created by init_db, absent from the raw schema.sql conn)
    # and run real ONNX inference. We exercise the agent, not the vector index.
    monkeypatch.setattr(embeddings, "upsert_note_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "delete_note_embedding", lambda *a, **k: None)
    if cid is None:
        cid = _conv(conn)

    async def go():
        out = []
        async for ev in arch.run(cid, user_text, mode=mode):
            out.append(ev)
        return out

    return asyncio.run(go()), cid


def _texts(events):
    return [e for e in events if e.get("type") == "token"]


def _types(events):
    return [e["type"] for e in events]


# --- 1: plain answer, no tools (loop terminates immediately) ----------------

def test_no_tool_turn_persists_reply(conn, monkeypatch):
    p = FakeProvider(script=[("Hello there.", [])])
    events, cid = _drive(conn, monkeypatch, p, "hi")
    assert _types(events)[-1] == "done"
    assert "".join(e["text"] for e in _texts(events)) == "Hello there."
    # The user turn AND the assistant reply are both persisted to the conversation.
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["content"] == "Hello there."


def test_no_credentials_yields_error(conn, monkeypatch):
    class NoCreds(FakeProvider):
        def has_credentials(self):
            return False
    events, _ = _drive(conn, monkeypatch, NoCreds(), "hi")
    assert events == [{"type": "error", "message": "No LLM API key configured."}]


# --- 2: a read tool is dispatched and its result is fed back ----------------

def test_read_note_tool_dispatched(conn, monkeypatch):
    conn.execute("INSERT INTO notes (slug, title, content_md, kind) VALUES "
                 "('a','notes/Travel/Chicago','Marathon expo was downtown.','entry')")
    conn.commit()
    p = FakeProvider(script=[
        ("Let me look.", [ToolCall(id="1", name="read_note", args={"title": "notes/Travel/Chicago"})]),
        ("It was the marathon expo.", []),
    ])
    events, cid = _drive(conn, monkeypatch, p, "what was the chicago trip")
    # The tool-status event fired for the dispatched read tool.
    assert any(e.get("type") == "tool" and e.get("tool") == "read_note" for e in events)
    # The tool's actual note content was handed back to the model (fenced + fetch-stamped).
    assert any("Marathon expo was downtown" in r for r in p.tool_results)
    # The reply persisted, and a tool step was recorded.
    steps = conn.execute("SELECT tool_name FROM message_steps WHERE conversation_id=?", (cid,)).fetchall()
    assert [s["tool_name"] for s in steps] == ["read_note"]


# --- 3: a forged / out-of-mode tool name is refused, never dispatched -------

def test_forged_tool_is_refused_not_run(conn, monkeypatch):
    p = FakeProvider(script=[
        ("", [ToolCall(id="1", name="rm_rf_everything", args={})]),
        ("ok", []),
    ])
    events, _ = _drive(conn, monkeypatch, p, "do something forbidden")
    # The dispatcher fails closed: the refusal text is handed back, nothing executed.
    assert any("is not available in assisted mode" in r for r in p.tool_results)


def test_write_tool_refused_in_research_mode(conn, monkeypatch):
    # A write tool the research mode never advertises must be refused even if the model names it.
    p = FakeProvider(script=[
        ("", [ToolCall(id="1", name="add_list_item",
                       args={"list_title": "Groceries", "item": "milk"})]),
        ("done", []),
    ])
    events, cid = _drive(conn, monkeypatch, p, "add milk to groceries", mode="research")
    assert any("is not available in research mode" in r for r in p.tool_results)
    # Nothing was written: no list note, no applied-action row.
    assert conn.execute("SELECT COUNT(*) FROM staging_actions").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM notes WHERE title LIKE 'lists/%'").fetchone()[0] == 0


def test_research_mode_offers_only_read_tools(conn, monkeypatch):
    p = FakeProvider(script=[("nothing to add", [])])
    _drive(conn, monkeypatch, p, "just chat", mode="research")
    # The mode boundary is enforced at the tool list, too: no write/propose tool is offered.
    assert "propose_actions" not in p.tools_seen
    assert "add_list_item" not in p.tools_seen
    assert "search_notes" in p.tools_seen


# --- 4: staged-write path (propose_actions) ---------------------------------

def test_propose_actions_stages_payload(conn, monkeypatch):
    p = FakeProvider(script=[
        ("Proposing.", [ToolCall(id="1", name="propose_actions", args={"actions": [
            {"type": "CREATE", "title": "notes/Ideas/New", "content": "# New\nbody",
             "summary": "create New"}]})]),
        ("Staged it for you.", []),
    ])
    events, cid = _drive(conn, monkeypatch, p, "make a note")
    # The staging event is surfaced to the client...
    staging = [e for e in events if e.get("type") == "staging"]
    assert staging and staging[0]["actions"][0]["type"] == "CREATE"
    # ...and a pending (not applied) staging row exists in the DB.
    rows = conn.execute(
        "SELECT type, status FROM staging_actions WHERE conversation_id=?", (cid,)).fetchall()
    assert len(rows) == 1 and rows[0]["type"] == "CREATE"
    assert rows[0]["status"] != "applied"
    # Nothing was actually written to notes — staging never mutates content.
    assert conn.execute(
        "SELECT COUNT(*) FROM notes WHERE title='notes/Ideas/New'").fetchone()[0] == 0


def test_propose_actions_rejects_malformed_action(conn, monkeypatch):
    # A RENAME missing new_title is validated up front: nothing staged, model told to fix it.
    p = FakeProvider(script=[
        ("", [ToolCall(id="1", name="propose_actions", args={"actions": [
            {"type": "RENAME", "title": "notes/Old", "summary": "rename"}]})]),
        ("ok", []),
    ])
    events, cid = _drive(conn, monkeypatch, p, "rename it")
    assert any("Nothing staged" in r and "new_title" in r for r in p.tool_results)
    assert conn.execute("SELECT COUNT(*) FROM staging_actions").fetchone()[0] == 0


# --- 5: a quick additive op auto-applies AND records an Undo ----------------

def test_add_list_item_applies_and_records_undo(conn, monkeypatch):
    p = FakeProvider(script=[
        ("", [ToolCall(id="1", name="add_list_item",
                       args={"list_title": "Groceries", "item": "milk"})]),
        ("Added milk.", []),
    ])
    events, cid = _drive(conn, monkeypatch, p, "add milk to groceries")
    # The applied event surfaced to the client.
    applied = [e for e in events if e.get("type") == "applied"]
    assert applied and "milk" in applied[0]["action"]["summary"]
    # The list note was really created/written.
    note = conn.execute(
        "SELECT content_md FROM notes WHERE title LIKE 'lists/%' AND deleted_at IS NULL").fetchone()
    assert note is not None and "milk" in note["content_md"]
    # A status='applied' row carries the inverse op so Undo can reverse it.
    row = conn.execute(
        "SELECT type, status, payload_json FROM staging_actions WHERE conversation_id=? "
        "AND status='applied'", (cid,)).fetchone()
    assert row["type"] == "ADD_ITEM"
    assert '"undo"' in row["payload_json"] and "remove_line" in row["payload_json"]


def test_log_entry_applies_and_persists_event(conn, monkeypatch):
    p = FakeProvider(script=[
        ("", [ToolCall(id="1", name="log_entry",
                       args={"target": "Journal", "text": "felt good today"})]),
        ("Logged.", []),
    ])
    events, cid = _drive(conn, monkeypatch, p, "log that I felt good")
    assert any(e.get("type") == "applied" for e in events)
    # The append landed in a real log note and an 'event' message row marks it in the chat.
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='event'",
        (cid,)).fetchone()[0] >= 1
    row = conn.execute("SELECT status FROM staging_actions WHERE type='LOG'").fetchone()
    assert row["status"] == "applied"


# --- 6: a tool that raises is caught, fed back, and the turn still completes -

def test_tool_exception_is_recovered(conn, monkeypatch):
    # read_note requires a 'title' arg; omitting it raises KeyError inside dispatch, which the
    # loop must catch and feed back as a tool result rather than killing the stream.
    p = FakeProvider(script=[
        ("", [ToolCall(id="1", name="read_note", args={})]),
        ("Recovered and answered.", []),
    ])
    events, cid = _drive(conn, monkeypatch, p, "read something")
    assert _types(events)[-1] == "done"
    assert any("failed" in r for r in p.tool_results)
    # The error step is persisted flagged as an error.
    step = conn.execute(
        "SELECT is_error FROM message_steps WHERE conversation_id=? AND tool_name='read_note'",
        (cid,)).fetchone()
    assert step["is_error"] == 1


# --- 7: step/token limit -> auto-continue notice (the truncation branch) ----

def test_iteration_limit_appends_continue_notice(conn, monkeypatch):
    # Shrink the per-turn step budget so a model that ALWAYS wants a tool exhausts it fast.
    from app.services import prompts
    real_get_int = prompts.get_int
    monkeypatch.setattr(prompts, "get_int",
                        lambda key, default: 2 if key.endswith("max_iterations") else real_get_int(key, default))
    conn.execute("INSERT INTO notes (slug,title,content_md,kind) VALUES "
                 "('a','notes/X','body','entry')")
    conn.commit()
    p = FakeProvider(always=("working…", [ToolCall(id="x", name="read_note", args={"title": "notes/X"})]))
    events, cid = _drive(conn, monkeypatch, p, "keep going forever")
    # The loop terminated (bounded) and told the user it stopped at the limit.
    full = "".join(e["text"] for e in _texts(events))
    assert "reached this turn's step/token limit" in full
    assert _types(events)[-1] == "done"
    # It honoured the shrunk budget — exactly the bound number of model turns ran.
    assert p.turns == 2


def test_token_budget_stops_loop(conn, monkeypatch):
    # A tiny cumulative token budget trips the cost backstop after the first tool round.
    from app.services import prompts
    real_get_int = prompts.get_int

    def fake_get_int(key, default):
        if key.endswith("max_total_tokens"):
            return 1
        if key.endswith("max_iterations"):
            return 8
        return real_get_int(key, default)
    monkeypatch.setattr(prompts, "get_int", fake_get_int)
    conn.execute("INSERT INTO notes (slug,title,content_md,kind) VALUES ('a','notes/X','body','entry')")
    conn.commit()
    p = FakeProvider(always=("…", [ToolCall(id="x", name="read_note", args={"title": "notes/X"})]))
    events, _ = _drive(conn, monkeypatch, p, "go")
    full = "".join(e["text"] for e in _texts(events))
    assert "reached this turn's step/token limit" in full
    # The budget tripped after the FIRST tool round, before a second model turn.
    assert p.turns == 1


# --- 8: reply truth-verification still runs through the loop ----------------

def test_fabricated_link_is_neutralized_in_persisted_reply(conn, monkeypatch):
    conn.execute("INSERT INTO notes (slug,title,content_md,kind) VALUES "
                 "('a','notes/Real','content','entry')")
    conn.commit()
    p = FakeProvider(script=[("See [[notes/Real]] and [[notes/Totally/Fake]].", [])])
    events, cid = _drive(conn, monkeypatch, p, "tell me")
    # The verifier asked the client to swap in the cleaned text...
    repl = [e for e in events if e.get("type") == "replace_text"]
    assert repl, "expected a replace_text event when a fabricated link is dropped"
    persisted = conn.execute(
        "SELECT content FROM messages WHERE conversation_id=? AND role='assistant'", (cid,)).fetchone()["content"]
    assert "[[notes/Real]]" in persisted          # the real link survives
    assert "[[notes/Totally/Fake]]" not in persisted  # the fabricated one is unwrapped
