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
  content_text TEXT NOT NULL DEFAULT '',   -- extracted searchable text (may be empty)
  content_blob BLOB,                        -- raw bytes (in-DB so backups are complete)
  byte_size    INTEGER NOT NULL DEFAULT 0,
  sha256       TEXT NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  analysis_status TEXT,                      -- NULL|pending|done|error (AI image analysis)
  analysis_detail TEXT,                      -- error message surfaced to the UI
  analyzed_at  TEXT                          -- when analysis last completed
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

-- PWA-editable action recipes (declarative multi-step pipelines). Repo recipes
-- are seeded from actions/*.yaml by type; a user edit sets locked=1 so repo
-- re-ingest won't clobber it. source='user' rows are custom actions with no repo
-- file behind them. The recipe body is stored verbatim (YAML text).
CREATE TABLE IF NOT EXISTS action_defs (
  type        TEXT PRIMARY KEY,                 -- canonical action type
  recipe_yaml TEXT NOT NULL,                    -- full recipe as authored (YAML)
  source      TEXT NOT NULL DEFAULT 'repo',      -- 'repo' | 'user'
  locked      INTEGER NOT NULL DEFAULT 0,        -- 1 = user-edited, freeze from re-ingest
  origin_hash TEXT,                              -- sha256 of repo file last ingested
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- PWA-editable prompt overrides (take precedence over prompts.yaml defaults).
CREATE TABLE IF NOT EXISTS prompt_overrides (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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

-- Public share links: an unguessable token granting unauthenticated single-note
-- access. scope 'view' = read that one note; 'edit' = read it AND submit proposals
-- (never a direct write). The token is stored as-is so the owner can re-copy the
-- link; revoking it kills access instantly.
CREATE TABLE IF NOT EXISTS share_links (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  token        TEXT UNIQUE NOT NULL,                -- 256-bit URL-safe; the capability itself
  note_id      INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  scope        TEXT NOT NULL CHECK (scope IN ('view','edit')),
  status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
  label        TEXT,
  expires_at   TEXT,
  bind         INTEGER NOT NULL DEFAULT 0,          -- 1 = lock to the first browser that ACCEPTS it
  bind_secret  TEXT,                                -- the bound browser's cookie value (set on accept)
  bound_at     TEXT,
  bound_name   TEXT,                                -- name the claimer gave (edit links), reused on proposals
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  revoked_at   TEXT,
  last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_share_links_note ON share_links(note_id);

-- An external editor's submission via an EDIT link, awaiting the owner's accept.
-- At most ONE pending row per share_link_id (a re-submission supersedes the prior).
CREATE TABLE IF NOT EXISTS share_proposals (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  share_link_id   INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
  note_id         INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  basis_hash      TEXT NOT NULL,                    -- sha256(note.content_md) at submit time
  proposed_content TEXT NOT NULL,
  proposer_name   TEXT,
  proposer_note   TEXT,
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending|accepted|rejected|superseded
  review_item_id  INTEGER REFERENCES review_items(id) ON DELETE SET NULL,
  client_ip       TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_share_prop_one_pending
  ON share_proposals(share_link_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_share_prop_status ON share_proposals(status);

-- Web Push subscriptions (one row per browser/device that opted in). The endpoint
-- is a push-service capability URL; p256dh/auth are the client's encryption keys.
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  endpoint     TEXT UNIQUE NOT NULL,
  p256dh       TEXT NOT NULL,
  auth         TEXT NOT NULL,
  ua           TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);
