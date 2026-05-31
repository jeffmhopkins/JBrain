"""SQLite connection, sqlite-vec loading, schema init, and admin seeding."""
import os
import sqlite3
import threading
from pathlib import Path

import sqlite_vec

from .config import get_settings

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per thread (sqlite connections are not thread-safe)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def _embedding_dim() -> int:
    # bge-small-en-v1.5 = 384. Keep in meta so we never mismatch the vec table.
    from .services.embeddings import EMBEDDING_DIM
    return EMBEDDING_DIM


SCHEMA_VERSION = 5


def init_db() -> None:
    """Create/upgrade schema + vec tables and seed meta. Idempotent.

    schema.sql is the full latest schema (all CREATE ... IF NOT EXISTS), so a
    fresh DB lands at the latest version directly. Existing DBs are upgraded by
    the migration runner, which applies guarded ALTERs for column additions that
    IF NOT EXISTS can't handle.
    """
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = get_conn()
        conn.executescript(SCHEMA_PATH.read_text())

        dim = _embedding_dim()
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_notes USING vec0("
            f"note_id INTEGER PRIMARY KEY, embedding float[{dim}])"
        )
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, embedding float[{dim}])"
        )

        _run_migrations(conn)

        settings = get_settings()
        set_meta(conn, "brain_name", settings.brain_name)
        set_meta(conn, "embedding_dim", str(dim))
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))

        conn.commit()
        _initialized = True


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    # SQLite has no ADD COLUMN IF NOT EXISTS, so guard explicitly.
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Upgrade an existing DB to SCHEMA_VERSION. Fresh DBs skip (already latest)."""
    raw = get_meta("schema_version")
    if raw is None:
        return  # brand-new DB: schema.sql already created everything at latest
    current = int(raw)

    if current < 3:
        # Revision-history columns + a baseline ("import") version per live note.
        _add_column(conn, "note_versions", "source", "TEXT NOT NULL DEFAULT 'user'")
        _add_column(conn, "note_versions", "conversation_id", "INTEGER")
        _add_column(conn, "note_versions", "note", "TEXT")
        conn.execute(
            "INSERT INTO note_versions (note_id, title, content_md, source, note) "
            "SELECT id, title, content_md, 'import', 'pre-migration snapshot' "
            "FROM notes WHERE deleted_at IS NULL"
        )


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(key: str, default: str | None = None) -> str | None:
    row = get_conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
