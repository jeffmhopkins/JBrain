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


def _connect(*, query_only: bool = False) -> sqlite3.Connection:
    settings = get_settings()
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait for a concurrent writer (WAL serialises writes) instead of failing the
    # request immediately with "database is locked".
    conn.execute("PRAGMA busy_timeout=5000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    if query_only:
        # Structurally block writes — defense in depth behind the SQL keyword
        # filter, so ad-hoc SELECTs can never mutate the DB even if the filter is
        # bypassed. (PRAGMA itself is rejected by the filter, so it can't be undone.)
        conn.execute("PRAGMA query_only=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per thread (sqlite connections are not thread-safe)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def get_query_conn() -> sqlite3.Connection:
    """A per-thread READ-ONLY connection for ad-hoc SQL (the SQL console and the
    research-mode query_sql tool). Writes are impossible through it."""
    conn = getattr(_local, "query_conn", None)
    if conn is None:
        conn = _connect(query_only=True)
        _local.query_conn = conn
    return conn


def _embedding_dim() -> int:
    # bge-small-en-v1.5 = 384. Keep in meta so we never mismatch the vec table.
    from .services.embeddings import EMBEDDING_DIM
    return EMBEDDING_DIM


SCHEMA_VERSION = 14


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

    if current < 6:
        # Location (+ time is already created_at) on entries and their sources.
        for table in ("notes", "messages", "inbox"):
            _add_column(conn, table, "lat", "REAL")
            _add_column(conn, table, "lon", "REAL")
            _add_column(conn, table, "location_label", "TEXT")

    if current < 7:
        # Knowledge-base layer marker (raw 'entry' vs synthesized 'kb').
        _add_column(conn, "notes", "kind", "TEXT NOT NULL DEFAULT 'entry'")

    if current < 9:
        # Store raw attachment bytes in the DB (any file type, not just text).
        _add_column(conn, "attachments", "content_blob", "BLOB")

    if current < 11:
        # Public share links + their pending edit proposals (idempotent creates;
        # schema.sql carries them for fresh DBs).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS share_links (
              id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL,
              note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
              scope TEXT NOT NULL CHECK (scope IN ('view','edit')),
              status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
              label TEXT, expires_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
              revoked_at TEXT, last_used_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_share_links_note ON share_links(note_id);
            CREATE TABLE IF NOT EXISTS share_proposals (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              share_link_id INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
              note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
              basis_hash TEXT NOT NULL, proposed_content TEXT NOT NULL, proposer_note TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              review_item_id INTEGER REFERENCES review_items(id) ON DELETE SET NULL,
              client_ip TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), resolved_at TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_share_prop_one_pending
              ON share_proposals(share_link_id) WHERE status = 'pending';
            CREATE INDEX IF NOT EXISTS idx_share_prop_status ON share_proposals(status);
        """)

    if current < 12:
        # Wiki synthesis now tracks entry CHANGES by timestamp (so edits + deletions
        # are caught), not just new note ids. Seed the new watermark from the old
        # one's note timestamp so we don't reprocess the whole history.
        old = get_meta("wiki_synth:last_note_id")
        if old and old != "0":
            row = conn.execute("SELECT updated_at FROM notes WHERE id = ?", (int(old),)).fetchone()
            now = conn.execute("SELECT datetime('now')").fetchone()[0]
            set_meta(conn, "wiki_synth:since", row["updated_at"] if row else now)

    if current < 13:
        # Editors of a share link now provide a name, shown in the owner's alert.
        _add_column(conn, "share_proposals", "proposer_name", "TEXT")

    if current < 14:
        # Optional "lock to first device" binding on share links.
        _add_column(conn, "share_links", "bind", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "share_links", "bind_secret", "TEXT")
        _add_column(conn, "share_links", "bound_at", "TEXT")


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(key: str, default: str | None = None) -> str | None:
    row = get_conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def backup_to_file(dest_path: str) -> None:
    """Write a consistent snapshot of the whole DB (WAL included) to dest_path."""
    dst = sqlite3.connect(dest_path)
    try:
        get_conn().backup(dst)
    finally:
        dst.close()


def restore_from_file(src_path: str) -> None:
    """Replace the live DB contents with those from src_path (a JBrain backup).

    Uses the backup API to copy pages into the live connection in-place, then
    re-runs init/migrations so an older backup is upgraded to the current schema.
    """
    global _initialized
    src = sqlite3.connect(src_path)
    try:
        ok = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('notes','meta')"
        ).fetchone()
        if not ok:
            raise ValueError("That file is not a JBrain database backup.")
        src.backup(get_conn())
    finally:
        src.close()
    # Re-apply schema/migrations on the restored data (upgrades older backups).
    _initialized = False
    init_db()
