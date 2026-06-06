"""The v44→v45 upgrade must create the message_steps table on an existing DB (fresh DBs get it
straight from schema.sql), and the migration must be idempotent."""
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

import app.db as db

SCHEMA = Path(db.__file__).parent / "schema.sql"


def _legacy_v44_conn():
    """A DB at the pre-feature shape: full schema minus message_steps, stamped schema_version 44."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    c.execute("DROP TABLE message_steps")       # roll back to the v44 shape
    db.set_meta(c, "schema_version", "44")
    c.commit()
    return c


def test_v45_migration_creates_message_steps(monkeypatch):
    c = _legacy_v44_conn()
    monkeypatch.setattr(db, "get_conn", lambda: c)   # so get_meta/set_meta target this conn
    assert not _table_exists(c, "message_steps")
    db._run_migrations(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(message_steps)")}
    assert {"conversation_id", "message_id", "step_index", "tool_name",
            "args_json", "result_text", "is_error", "event_json"} <= cols


def test_v45_migration_is_idempotent(monkeypatch):
    c = _legacy_v44_conn()
    monkeypatch.setattr(db, "get_conn", lambda: c)
    db._run_migrations(c)
    db._run_migrations(c)                            # second pass must not throw
    assert _table_exists(c, "message_steps")


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None
