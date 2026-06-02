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


SCHEMA_VERSION = 24


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

    if current < 15:
        # Name captured when a bind link is accepted (reused on the editor's proposals).
        _add_column(conn, "share_links", "bound_name", "TEXT")

    if current < 16:
        # Optional AI vision analysis of image attachments. Latest-status only
        # (no run history): a background worker sets these and the UI polls them.
        _add_column(conn, "attachments", "analysis_status", "TEXT")   # NULL|pending|done|error
        _add_column(conn, "attachments", "analysis_detail", "TEXT")   # error message for the UI
        _add_column(conn, "attachments", "analyzed_at", "TEXT")

    if current < 17:
        # Web Push subscriptions (idempotent create; schema.sql carries it for fresh DBs).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, endpoint TEXT UNIQUE NOT NULL,
              p256dh TEXT NOT NULL, auth TEXT NOT NULL, ua TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              last_seen_at TEXT NOT NULL DEFAULT (datetime('now')));
        """)

    if current < 18:
        # Guided AI intake links: a 'kind' marker on share_links, the owner-approved
        # interview spec, and per-recipient sessions (transcript + the AI-drafted doc).
        _add_column(conn, "share_links", "kind", "TEXT NOT NULL DEFAULT 'note'")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS guided_specs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              share_link_id INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
              goal TEXT NOT NULL DEFAULT '', intro TEXT NOT NULL DEFAULT '',
              sub_prompt TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active')),
              max_turns INTEGER NOT NULL DEFAULT 40,
              max_total_replies INTEGER NOT NULL DEFAULT 80,
              reply_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE UNIQUE INDEX IF NOT EXISTS idx_guided_specs_link ON guided_specs(share_link_id);
            CREATE TABLE IF NOT EXISTS guided_sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              share_link_id INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
              secret TEXT NOT NULL, name TEXT,
              status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','drafting','submitted','abandoned')),
              transcript_json TEXT NOT NULL DEFAULT '[]',
              document_md TEXT, turn_count INTEGER NOT NULL DEFAULT 0,
              review_item_id INTEGER REFERENCES review_items(id) ON DELETE SET NULL,
              client_ip TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), completed_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_guided_sessions_link ON guided_sessions(share_link_id);
        """)

    if current < 19:
        # Per-link options: lock to the first device, and run once to completion.
        _add_column(conn, "guided_specs", "bind", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "guided_specs", "single_use", "INTEGER NOT NULL DEFAULT 0")

    if current < 20:
        # Abuse/distress safeguard: a per-session strike counter and the terminal
        # reason (status stays 'abandoned'; end_reason distinguishes 'abuse:*'/'distress').
        _add_column(conn, "guided_sessions", "strike_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "guided_sessions", "end_reason", "TEXT")

    if current < 21:
        # Research links (kind='research'): scoped read-only Q&A. The boundary is the
        # approved note-id allowlist; the filter only surfaces candidates to approve.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS research_specs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              share_link_id INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
              status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active')),
              scope_json TEXT NOT NULL DEFAULT '{}',
              approved_ids_json TEXT NOT NULL DEFAULT '[]',
              dismissed_ids_json TEXT NOT NULL DEFAULT '[]',
              persona_voice TEXT NOT NULL DEFAULT '', intro TEXT NOT NULL DEFAULT '',
              bind INTEGER NOT NULL DEFAULT 0, single_use INTEGER NOT NULL DEFAULT 0,
              max_turns INTEGER NOT NULL DEFAULT 30, max_total_replies INTEGER NOT NULL DEFAULT 200,
              reply_count INTEGER NOT NULL DEFAULT 0, token_budget INTEGER NOT NULL DEFAULT 40000,
              created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE UNIQUE INDEX IF NOT EXISTS idx_research_specs_link ON research_specs(share_link_id);
            CREATE TABLE IF NOT EXISTS research_sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              share_link_id INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
              secret TEXT NOT NULL, name TEXT,
              status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','ended')),
              transcript_json TEXT NOT NULL DEFAULT '[]', retrieved_ids_json TEXT NOT NULL DEFAULT '[]',
              denied_count INTEGER NOT NULL DEFAULT 0, turn_count INTEGER NOT NULL DEFAULT 0,
              client_ip TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), last_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_research_sessions_link ON research_sessions(share_link_id);
        """)

    if current < 22:
        # Device location trail (opt-in background tracking from a native client).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS locations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              lat REAL NOT NULL, lon REAL NOT NULL, accuracy_m REAL,
              recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
              source TEXT NOT NULL DEFAULT 'wear',
              created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE INDEX IF NOT EXISTS idx_locations_recorded ON locations(recorded_at);
        """)

    if current < 23:
        # Places (geofences) + per-place state + per-workflow trigger dedup.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS places (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
              lat REAL NOT NULL, lon REAL NOT NULL, radius_m INTEGER NOT NULL DEFAULT 150,
              note_slug TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS location_state (
              place_id INTEGER PRIMARY KEY REFERENCES places(id) ON DELETE CASCADE,
              inside INTEGER NOT NULL DEFAULT 0, since TEXT,
              last_inside_at TEXT, last_fix_at TEXT);
            CREATE TABLE IF NOT EXISTS location_fired (
              workflow_id INTEGER NOT NULL, kind TEXT NOT NULL, marker TEXT,
              fired_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (workflow_id, kind));
        """)

    if current < 24:
        # LLM token-usage ledger (in-app cost awareness).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS llm_usage (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL DEFAULT (datetime('now')),
              model TEXT NOT NULL,
              input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              cache_read_tokens INTEGER NOT NULL DEFAULT 0,
              cache_write_tokens INTEGER NOT NULL DEFAULT 0,
              context TEXT);
            CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
        """)


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
