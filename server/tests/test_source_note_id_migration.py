"""Regression for the v46→v47 boot crash on existing installs.

schema.sql carries a partial index on article_talk.source_note_id, but that column is
added to the PRE-EXISTING article_talk table by migration v47. init_db must therefore
run the migration runner BEFORE applying schema.sql, or schema.sql crashes with:

    sqlite3.OperationalError: no such column: source_note_id

(observed in production while upgrading a pre-v47 database). Fresh DBs are unaffected
because their CREATE TABLE creates the column up front.
"""
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("sqlite_vec")

import app.db as db

SCHEMA = Path(db.__file__).parent / "schema.sql"


def _legacy_v46_conn():
    """Full current schema, with article_talk rolled back to its pre-v47 shape (no
    source_note_id / is_correction, no partial index), stamped schema_version 46."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    c.execute("DROP INDEX IF EXISTS idx_article_talk_source_note")
    c.execute("ALTER TABLE article_talk DROP COLUMN source_note_id")
    c.execute("ALTER TABLE article_talk DROP COLUMN is_correction")
    db.set_meta(c, "schema_version", "46")
    c.commit()
    return c


def _has_column(conn, table, col):
    return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _has_index(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone() is not None


def test_schema_sql_alone_crashes_on_pre_v47_db():
    """Documents the bug: applying schema.sql before the column migration fails."""
    c = _legacy_v46_conn()
    with pytest.raises(sqlite3.OperationalError, match="source_note_id"):
        c.executescript(SCHEMA.read_text())


def test_migrate_then_schema_upgrades_cleanly(monkeypatch):
    """The init_db order — migrations FIRST, then schema.sql — upgrades v46 without error."""
    c = _legacy_v46_conn()
    monkeypatch.setattr(db, "get_conn", lambda: c)   # so get_meta/set_meta target this conn
    db._run_migrations(c)                              # adds source_note_id + is_correction + index
    c.executescript(SCHEMA.read_text())                # must NOT raise now (index col exists)
    assert _has_column(c, "article_talk", "source_note_id")
    assert _has_column(c, "article_talk", "is_correction")
    assert _has_index(c, "idx_article_talk_source_note")


def test_run_migrations_noops_before_schema_on_fresh_db():
    """On a brand-new DB the runner executes BEFORE schema.sql, so the meta table does
    not exist yet — it must treat that as 'nothing to upgrade' rather than crashing."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db._run_migrations(c)   # no meta table → must return quietly, no exception
