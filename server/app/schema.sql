-- JBrain schema. Regular tables + FTS5. The sqlite-vec virtual table is created
-- in db.py (its dimension depends on the embedding model).

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS notes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT UNIQUE NOT NULL,
  slug       TEXT UNIQUE NOT NULL,
  content_md TEXT NOT NULL DEFAULT '',
  kind           TEXT NOT NULL DEFAULT 'entry',   -- 'entry' (raw) | 'kb' (synthesized)
  lat            REAL,
  lon            REAL,
  location_label TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

-- Full history. One row per authored state (created/updated/restored). The
-- NEWEST row equals the live note content. `source` = who authored THIS row's
-- content: 'user' | 'architect' | 'restore' | 'import'.
CREATE TABLE IF NOT EXISTS note_versions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  note_id         INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  title           TEXT NOT NULL,
  content_md      TEXT NOT NULL,
  source          TEXT NOT NULL DEFAULT 'user',
  conversation_id INTEGER,
  note            TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_note_versions_note ON note_versions(note_id);

-- Wiki-link edges. target_note_id is NULL until the target note exists.
CREATE TABLE IF NOT EXISTS links (
  source_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  target_note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL,
  target_title   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_note_id);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_note_id);
CREATE INDEX IF NOT EXISTS idx_links_target_title ON links(target_title);

CREATE TABLE IF NOT EXISTS tags (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS note_tags (
  note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (note_id, tag_id)
);

CREATE TABLE IF NOT EXISTS conversations (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT,
  started_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,            -- 'user' | 'assistant'
  content         TEXT NOT NULL,
  lat             REAL,
  lon             REAL,
  location_label  TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);

-- Proposed wiki changes awaiting explicit confirmation.
CREATE TABLE IF NOT EXISTS staging_actions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
  type            TEXT NOT NULL,            -- 'CREATE' | 'UPDATE' | 'LINK'
  payload_json    TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending', -- pending|applied|rejected
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_staging_status ON staging_actions(status);

-- Quick-capture inbox (e.g. dictation from a watch/phone) processed later.
CREATE TABLE IF NOT EXISTS inbox (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  source     TEXT NOT NULL DEFAULT 'capture',
  content    TEXT NOT NULL,
  lat            REAL,
  lon            REAL,
  location_label TEXT,
  processed  INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Standalone full-text index (kept in sync manually on note save).
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  note_id UNINDEXED,
  title,
  content
);

-- File attachments (text/markdown in v1). Content is stored as TEXT so it lives
-- in one consistency domain and is trivially searchable. note_id is nullable so
-- an attachment can exist before being linked to an entry.
CREATE TABLE IF NOT EXISTS attachments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  note_id      INTEGER REFERENCES notes(id) ON DELETE CASCADE,
  filename     TEXT NOT NULL,
  mime         TEXT NOT NULL,
  content_text TEXT NOT NULL DEFAULT '',
  byte_size    INTEGER NOT NULL DEFAULT 0,
  sha256       TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_attachments_note ON attachments(note_id);
CREATE INDEX IF NOT EXISTS idx_attachments_sha  ON attachments(note_id, sha256);

-- Chunk metadata for attachment semantic search. The matching float vectors are
-- stored in the vec_chunks virtual table (created in db.py), keyed by this id.
CREATE TABLE IF NOT EXISTS attachment_chunks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
  note_id       INTEGER,
  chunk_index   INTEGER NOT NULL,
  text          TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chunks_att ON attachment_chunks(attachment_id);

-- Full-text index over attachment content (separate from notes_fts).
CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5(
  attachment_id UNINDEXED,
  note_id       UNINDEXED,
  filename,
  content
);

-- Workflows: trigger + action automations. Seeded from repo YAML, then editable
-- in the PWA (a user edit sets `locked` so repo re-ingest won't clobber it).
CREATE TABLE IF NOT EXISTS workflows (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  key           TEXT UNIQUE,                       -- stable id for repo workflows
  name          TEXT NOT NULL,
  trigger_type  TEXT NOT NULL,                     -- 'event' | 'schedule'
  trigger_config TEXT NOT NULL DEFAULT '{}',       -- json
  action_type   TEXT NOT NULL,
  action_config TEXT NOT NULL DEFAULT '{}',        -- json
  enabled       INTEGER NOT NULL DEFAULT 1,
  source        TEXT NOT NULL DEFAULT 'repo',       -- 'repo' | 'user'
  locked        INTEGER NOT NULL DEFAULT 0,         -- 1 = user-edited, freeze from re-ingest
  origin_hash   TEXT,
  last_run_at   TEXT,
  last_status   TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Review items: workflow-posted (or manual) cards surfaced in the PWA Review
-- inbox — a title, a message, an optional link to an entry, and a dismiss.
CREATE TABLE IF NOT EXISTS review_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id  INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
  title        TEXT NOT NULL,
  message      TEXT,
  link_slug    TEXT,                                -- note slug to open, if any
  status       TEXT NOT NULL DEFAULT 'pending',     -- 'pending' | 'dismissed'
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  dismissed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(status);

-- Audit log of workflow executions.
CREATE TABLE IF NOT EXISTS workflow_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  started_at  TEXT NOT NULL DEFAULT (datetime('now')),
  status      TEXT NOT NULL,                        -- 'ok' | 'error' | 'skipped'
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_wf ON workflow_runs(workflow_id);
