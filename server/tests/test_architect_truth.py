"""Truth/freshness guarantees for the owner chat agent: a fabricated [[link]] or URL must never
survive into the persisted reply, and a returning/idle user must not get stale earlier answers
re-fed to the model as authoritative."""
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")
pytest.importorskip("anthropic")

from app.services import architect

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    c.execute("INSERT INTO notes (slug, title, content_md) VALUES ('r', 'notes/Medical/Cholesterol', 'b')")
    c.commit()
    return c


# --- link / URL verification (the hard guarantee) ---------------------------

def test_verify_reply_neutralizes_fabricated_links(conn):
    text = "Per [[notes/Medical/Cholesterol]] it's high, and see [[notes/Made/Up]] too."
    out, changed = architect._verify_reply(conn, text, set())
    assert changed
    assert "[[notes/Medical/Cholesterol]]" in out          # real link survives
    assert "[[notes/Made/Up]]" not in out and "Up" in out  # fake link unwrapped to plain text


def test_verify_reply_keeps_real_links_untouched(conn):
    text = "See [[notes/Medical/Cholesterol]]."
    out, changed = architect._verify_reply(conn, text, set())
    assert not changed and out == text


def test_verify_reply_strips_unsourced_urls(conn):
    text = "Info at https://medlineplus.gov/fake and also [here](https://evil.example/x)."
    out, changed = architect._verify_reply(conn, text, set())
    assert changed
    assert "medlineplus.gov" not in out and "evil.example" not in out
    assert "[here]" not in out and "here" in out           # markdown label kept, fabricated URL dropped


def test_verify_reply_keeps_tool_returned_url(conn):
    url = "https://medlineplus.gov/druginfo/meds/a696005.html"
    out, changed = architect._verify_reply(conn, f"Reference: {url}", {url})
    assert not changed and url in out                      # a URL a tool actually returned is allowed


def test_extract_urls_normalizes():
    assert architect._extract_urls("see https://x.com/a, and https://y.org.") == {
        "https://x.com/a", "https://y.org"}


# --- freshness window -------------------------------------------------------

def _add(conn, cid, role, content, created_at=None):
    if created_at:
        conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?,?,?,?)",
                     (cid, role, content, created_at))
    else:
        conn.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)",
                     (cid, role, content))


def _conv(conn):
    conn.execute("INSERT INTO conversations DEFAULT VALUES")
    return conn.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_recent_history_is_kept_but_assistant_tagged_stale(conn):
    cid = _conv(conn)
    _add(conn, cid, "user", "what was my wbc")
    _add(conn, cid, "assistant", "your WBC was 11.2")
    conn.commit()
    h = architect._assemble_history(conn, cid, fresh_context=False)
    assert len(h) == 2 and h[0]["role"] == "user"
    assert "earlier turn" in h[1]["content"] and "11.2" in h[1]["content"]   # stale-tagged, not dropped


def test_fresh_context_flag_clears_history(conn):
    cid = _conv(conn)
    _add(conn, cid, "user", "hi")
    _add(conn, cid, "assistant", "your WBC was 11.2")
    conn.commit()
    assert architect._assemble_history(conn, cid, fresh_context=True) == []   # left app / new topic


def test_large_time_gap_clears_history(conn):
    cid = _conv(conn)
    _add(conn, cid, "user", "hi", created_at="2020-01-01 00:00:00")
    _add(conn, cid, "assistant", "old answer 11.2", created_at="2020-01-01 00:00:05")
    conn.commit()
    assert architect._assemble_history(conn, cid, fresh_context=False) == []  # resumed after a break


def test_empty_history(conn):
    assert architect._assemble_history(conn, _conv(conn), fresh_context=False) == []
