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

-- Per-note semantic chunks. The whole-note vector in vec_notes truncates at the
-- embedder's ~512-token limit, so a long note's body never gets embedded; we ALSO
-- split each note into windows (mirroring attachment_chunks) and embed each into
-- vec_note_chunks, so semantic_search collapses to a note's best-matching chunk.
-- vec_notes stays (one whole-note vector per note) — research_scope reads it directly.
CREATE TABLE IF NOT EXISTS note_chunks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  note_id     INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  text        TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_note_chunks_note ON note_chunks(note_id);

-- Cached per-note AI analysis (structured signals: gist, salient facts, entities,
-- domain guess). A SIDECAR — it never mutates the note body, so the raw note stays
-- the source of truth. Keyed by content_hash so it's recomputed only when the note
-- actually changes. Feeds the KB pipeline + a read-only panel in the note view.
CREATE TABLE IF NOT EXISTS note_analysis (
  note_id       INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
  content_hash  TEXT NOT NULL,
  gist          TEXT NOT NULL DEFAULT '',
  facts_json    TEXT NOT NULL DEFAULT '[]',
  entities_json TEXT NOT NULL DEFAULT '[]',
  domain        TEXT,
  dates_json    TEXT NOT NULL DEFAULT '[]',
  model         TEXT,
  analyzed_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Canonical entity index, AGGREGATED from note_analysis entities (a derived index,
-- like FTS). One row per real-world person/org/place/thing, with name variants merged
-- conservatively. Feeds the KB outline (recurring entities + co-occurrence -> articles
-- & Groups clustering) and a browse view. Upserted by (type, normalized_key) so ids are
-- stable across rebuilds.
CREATE TABLE IF NOT EXISTS entities (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  type           TEXT NOT NULL,                       -- person | org | place | thing
  canonical_name TEXT NOT NULL,
  normalized_key TEXT NOT NULL,
  note_count     INTEGER NOT NULL DEFAULT 0,
  article_title  TEXT,                                -- the kb article for this entity, if one exists
  updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(type, normalized_key)
);
CREATE TABLE IF NOT EXISTS entity_mentions (
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  note_id   INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  raw_name  TEXT,
  PRIMARY KEY (entity_id, note_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_note ON entity_mentions(note_id);



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
  note_id      INTEGER REFERENCES notes(id) ON DELETE CASCADE,   -- NULL for guided/research (no page until accepted)
  scope        TEXT NOT NULL CHECK (scope IN ('view','edit')),
  kind         TEXT NOT NULL DEFAULT 'note',          -- 'note' (view/edit a note) | 'guided' (AI intake)
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

-- Guided AI intake: the owner-approved interview spec for a 'guided' share link.
-- sub_prompt is the goal-specific instructions for the recipient-facing interview
-- AI (wrapped at runtime by a fixed safety preamble). status draft->active is the
-- owner's FIRST approval gate (the link is inert until 'active').
CREATE TABLE IF NOT EXISTS guided_specs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  share_link_id   INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
  goal            TEXT NOT NULL DEFAULT '',           -- owner's stated goal (audit/UI)
  intro           TEXT NOT NULL DEFAULT '',           -- what the recipient sees on the consent landing
  sub_prompt      TEXT NOT NULL,                      -- generated instructions for the interview AI
  dest_title      TEXT NOT NULL DEFAULT '',           -- where the accepted doc lands (note created on accept)
  status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active')),
  bind            INTEGER NOT NULL DEFAULT 0,         -- lock to the first device that begins it
  single_use      INTEGER NOT NULL DEFAULT 0,         -- close after one completed response
  max_turns       INTEGER NOT NULL DEFAULT 40,        -- per-session recipient-AI replies
  max_total_replies INTEGER NOT NULL DEFAULT 80,      -- cumulative across the link (hard cost cap)
  reply_count     INTEGER NOT NULL DEFAULT 0,         -- billed AI replies so far (atomic counter)
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_guided_specs_link ON guided_specs(share_link_id);

-- One recipient's run through a guided link: the transcript and the AI-drafted
-- document awaiting the owner's SECOND approval.
CREATE TABLE IF NOT EXISTS guided_sessions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  share_link_id   INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
  secret          TEXT NOT NULL,                      -- httponly cookie tying this browser to the session
  name            TEXT,
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','drafting','submitted','abandoned')),
  transcript_json TEXT NOT NULL DEFAULT '[]',
  document_md     TEXT,                               -- the AI-synthesized document (for owner review)
  turn_count      INTEGER NOT NULL DEFAULT 0,
  strike_count    INTEGER NOT NULL DEFAULT 0,         -- abuse de-escalation ladder (redirect/warn/end)
  end_reason      TEXT,                               -- 'abuse:<reason>' | 'distress' when auto-ended
  review_item_id  INTEGER REFERENCES review_items(id) ON DELETE SET NULL,
  client_ip       TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_guided_sessions_link ON guided_sessions(share_link_id);

-- Research links (kind='research'): a scoped, read-only Q&A link. The exposed
-- boundary is the APPROVED note-id allowlist (approved_ids_json) — never the live
-- filter (scope_json), which only surfaces candidates for the owner to approve.
-- status draft->active is the owner's approval gate; persona_voice is an optional
-- tone string interpolated into a FIXED template (it can't countermand the rules).
CREATE TABLE IF NOT EXISTS research_specs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  share_link_id     INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
  status            TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active')),
  scope_json        TEXT NOT NULL DEFAULT '{}',        -- candidate FILTER {prefixes:[],kinds:[]}
  approved_ids_json TEXT NOT NULL DEFAULT '[]',        -- the exposed allowlist (the ONLY boundary)
  dismissed_ids_json TEXT NOT NULL DEFAULT '[]',       -- candidates the owner rejected (don't re-nag)
  persona_voice     TEXT NOT NULL DEFAULT '',          -- optional tone/role; '' = neutral default
  topics            TEXT NOT NULL DEFAULT '',          -- owner's discussion scope (what to/not discuss)
  intro             TEXT NOT NULL DEFAULT '',          -- recipient consent-landing text
  bind              INTEGER NOT NULL DEFAULT 0,
  single_use        INTEGER NOT NULL DEFAULT 0,
  max_turns         INTEGER NOT NULL DEFAULT 30,       -- per-session answers
  max_total_replies INTEGER NOT NULL DEFAULT 200,      -- cumulative across the link (cost cap)
  reply_count       INTEGER NOT NULL DEFAULT 0,        -- atomic billed-answer counter
  token_budget      INTEGER NOT NULL DEFAULT 40000,    -- per-turn cumulative token cap
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_specs_link ON research_specs(share_link_id);

-- One recipient's Q&A run through a research link: transcript + an audit log of
-- exactly which notes informed answers, and a counter of out-of-scope attempts.
CREATE TABLE IF NOT EXISTS research_sessions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  share_link_id     INTEGER NOT NULL REFERENCES share_links(id) ON DELETE CASCADE,
  secret            TEXT NOT NULL,                     -- httponly cookie tying this browser to the session
  name              TEXT,
  status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','ended')),
  transcript_json   TEXT NOT NULL DEFAULT '[]',
  retrieved_ids_json TEXT NOT NULL DEFAULT '[]',       -- audit: notes that informed answers
  denied_count      INTEGER NOT NULL DEFAULT 0,        -- audit: out-of-scope retrieval attempts
  turn_count        INTEGER NOT NULL DEFAULT 0,
  client_ip         TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  last_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_sessions_link ON research_sessions(share_link_id);

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

-- Device location trail (opt-in background tracking from a native client). The
-- server enforces the "store a point only if >=100 m moved OR >=60 min elapsed"
-- rule authoritatively, so clients/retries/offline-flushes can't create dupes.
CREATE TABLE IF NOT EXISTS locations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  lat         REAL NOT NULL,
  lon         REAL NOT NULL,
  accuracy_m  REAL,
  recorded_at TEXT NOT NULL DEFAULT (datetime('now')),   -- when the fix was taken (UTC)
  source      TEXT NOT NULL DEFAULT 'wear',
  -- Resolved person (cache of source->people.aliases) so trip detection can query a
  -- person's stream in SQL; re-resolved when people/aliases change. NULL until resolved.
  person_id   INTEGER,
  -- Device-reported motion (GPS Doppler), all optional/nullable for back-compat:
  speed_mps   REAL,                                       -- ground speed, m/s
  bearing_deg REAL,                                       -- heading, degrees (0-360)
  altitude_m  REAL,                                       -- metres above sea level
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))     -- when the server received it
);
CREATE INDEX IF NOT EXISTS idx_locations_recorded ON locations(recorded_at);
-- NOTE: the idx_locations_person_time index (on the migration-added person_id column)
-- is created in ensure_default_person(), AFTER migrations — never here, since this
-- script runs BEFORE migrations and person_id won't exist yet on an upgraded DB.

-- Detected TRIPS: the moving segment between two stays, per person. A derived cache
-- recomputed idempotently from the raw fix stream; place fields are denormalised
-- SNAPSHOTS (places are editable/deletable) so a past trip's "started at Home" is
-- historical truth, not a live join.
CREATE TABLE IF NOT EXISTS trips (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id       INTEGER REFERENCES people(id) ON DELETE SET NULL,
  source          TEXT,                  -- the fix source this trip was segmented from
  started_at      TEXT NOT NULL,         -- UTC, departure
  ended_at        TEXT NOT NULL,         -- UTC, arrival (== last fix while status='open')
  start_lat       REAL, start_lon REAL,
  end_lat         REAL, end_lon REAL,
  start_place_id  INTEGER REFERENCES places(id) ON DELETE SET NULL,
  start_place     TEXT,                  -- snapshot label (place/coord-note/NULL)
  end_place_id    INTEGER REFERENCES places(id) ON DELETE SET NULL,
  end_place       TEXT,
  distance_km     REAL NOT NULL DEFAULT 0,
  displacement_km REAL NOT NULL DEFAULT 0,  -- straight-line start->end (for directness)
  duration_s      INTEGER NOT NULL DEFAULT 0,
  max_speed_kmh   REAL,
  avg_speed_kmh   REAL,
  fix_count       INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'closed' CHECK (status IN ('open','closed')),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trips_person_time ON trips(person_id, started_at);
CREATE INDEX IF NOT EXISTS idx_trips_started ON trips(started_at);

-- Geofences a trip PASSED THROUGH (excludes its start/end), with dwell. Place name +
-- coords snapshotted at detection time.
CREATE TABLE IF NOT EXISTS trip_places (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id    INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  place_id   INTEGER REFERENCES places(id) ON DELETE SET NULL,
  place_name TEXT,
  lat        REAL, lon REAL,
  entered_at TEXT, left_at TEXT,
  dwell_s    INTEGER NOT NULL DEFAULT 0,
  ordinal    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trip_places_trip ON trip_places(trip_id);

-- Per-person trip-detection progress: a UTC watermark (segmentation is re-run from
-- here forward, deleting+recreating overlapping trips, so late/out-of-order fixes just
-- move the watermark back) and how far back history has been backfilled.
CREATE TABLE IF NOT EXISTS trip_cursor (
  person_id      INTEGER PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
  watermark      TEXT,                  -- recorded_at processed up to (closed trips only)
  backfilled_to  TEXT,                  -- oldest recorded_at considered (backfill floor)
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Named geofences for location tools + triggers (notes have lat/lon but no radius).
CREATE TABLE IF NOT EXISTS places (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  lat        REAL NOT NULL,
  lon        REAL NOT NULL,
  radius_m   INTEGER NOT NULL DEFAULT 150,
  note_slug  TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Physical "am I inside this place?" truth, updated cheaply on each kept fix. The
-- scheduler (never the ingest path) reads this to fire location triggers.
CREATE TABLE IF NOT EXISTS location_state (
  place_id       INTEGER PRIMARY KEY REFERENCES places(id) ON DELETE CASCADE,
  inside         INTEGER NOT NULL DEFAULT 0,
  since          TEXT,                 -- when the current inside/outside state began
  last_inside_at TEXT,                 -- last fix inside this place (for 'away')
  last_fix_at    TEXT
);

-- Per-workflow dedup so a trigger fires once per episode (marker = the state's `since`).
CREATE TABLE IF NOT EXISTS location_fired (
  workflow_id INTEGER NOT NULL,
  kind        TEXT NOT NULL,           -- 'dwell' | 'away' | 'arrived' | 'left' | 'new_place'
  marker      TEXT,
  fired_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (workflow_id, kind)
);

-- People whose data appears in the brain (NOT auth accounts — JBrain stays single
-- access key). A person attributes/colours location trails (matched from a fix's
-- `source` via `aliases`) and can be linked to a KB page (note_slug). Exactly one row
-- is the default ("Me") — the catch-all for any unmatched source.
CREATE TABLE IF NOT EXISTS people (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT UNIQUE NOT NULL,
  color        TEXT NOT NULL DEFAULT '#7f9aa6',
  is_default   INTEGER NOT NULL DEFAULT 0,
  aliases      TEXT NOT NULL DEFAULT '',        -- comma-separated source aliases
  note_slug    TEXT,                            -- optional linked KB page
  location_key TEXT,                            -- scoped token: location-WRITE only, forces source=this person
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
-- NOTE: the UNIQUE index on location_key is created in ensure_default_person(), NOT
-- here. schema.sql runs BEFORE migrations, and an existing people table (from
-- migration 27, pre-location_key) won't have the column yet — indexing it here would
-- crash with "no such column" on upgrade.

-- LLM token-usage ledger: one row per provider call (recorded on a dedicated
-- connection so it never touches the caller's transaction). Token counts are
-- exact; the dollar figure derived from them is an ESTIMATE (see services/usage.py).
CREATE TABLE IF NOT EXISTS llm_usage (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                 TEXT NOT NULL DEFAULT (datetime('now')),   -- UTC
  model              TEXT NOT NULL,
  input_tokens       INTEGER NOT NULL DEFAULT 0,
  output_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  context            TEXT                                        -- 'agent' | 'action' (informational)
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
