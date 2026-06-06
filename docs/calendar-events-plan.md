# Appointment / Calendar Side-Database — Build Spec (v1, draft)

Status: **DRAFT FOR REVIEW.** No code written yet. This spec proposes a queryable
*temporal projection* — a "calendar" sidecar that collects appointments,
deadlines, reminders, and recurring patterns out of note bodies (and lets you add
some by hand), each row linking back to its source note. Grounding citations are
`file:line` against the tree at writing.

## 0. Goal & position

Make **time** queryable the same way labs and entities already are. Today the
brain extracts a `dates_json` signal from every note
(`server/app/schema.sql:120`, `server/app/services/note_analysis.py:40,149` — the
analyzer is literally told to emit `"dates":["YYYY-MM-DD: what happened"]`), but
that signal goes nowhere collectable. You cannot ask "what's coming up", "when is
my next appointment with Dr. X", or "how often does this recur" without an LLM
re-reading many notes.

This spec adds a **`calendar_events`** projection table that:

1. **Collects** the temporal signal already being extracted, plus an explicit
   extraction pass, into typed, queryable rows.
2. **Links every row back to its source note** (`note_id` provenance), exactly
   like `lab_results` / `encounters` / `entities`.
3. **Stays a projection, never the source of truth** — re-derivable from notes,
   with a thin, clearly-flagged manual-entry escape hatch.
4. **Surfaces what's coming up** through the existing Review inbox **and Web
   Push** (the two reminder channels chosen for v1).

### Position relative to existing systems

This is the **established JBrain sidecar pattern**, pointed at time:

| Existing | This spec |
|---|---|
| `lab_results` projects clinical numbers from notes/attachments (`schema.sql:723`) | `calendar_events` projects dated commitments from notes |
| `entities` aggregates people/orgs from `note_analysis` (`schema.sql:130`) | reused: an event links to its `entity_id` (the doctor/org) |
| `encounters` is a spine with `note_slug` + `identity_key` dedup (`schema.sql:684`) | same conventions: `note_id` provenance + `identity_key` upsert |
| `places` / `trips` earned their own tab + tools (`schema.sql:499,549`) | a **Calendar** surface earns the same |
| `promote_recurrences` clusters repeated chatter into kb/Patterns (`actions/promote_recurrences.yaml`) | the *recurrence detector* feeds calendar rows with an `rrule` |
| `review_items` inbox + `push_subscriptions` (`schema.sql:264,461`) | the **two reminder channels** for upcoming/recurring items |

**The one real tension (state it honestly):** JBrain's discipline is *notes are
truth; sidecars are derived* (`schema.sql:111-112` "A SIDECAR — it never mutates
the note body, so the raw note stays the source of truth"). A calendar invites
drift toward "the place you edit appointments directly." v1 resists this:
extracted rows are **re-derivable**; manual rows are a flagged exception
(`source='manual'`) and, where possible, also drop a `[[wiki-link]]` back into a
note so the note remains the durable record. If this ever becomes the
authoritative store you edit instead of notes, it is fighting the rest of the
system — that boundary is the thing to watch in review.

---

## 1. Schema

### 1.1 The projection table

Named **`calendar_events`**, NOT `events` — `events` is already the app-event
router namespace (`server/app/routers/events.py:17`, `/api/events`,
`wiki_viewed`), an unrelated "client fired a UI signal" concept. Reusing the word
would be a footgun.

```sql
-- A queryable PROJECTION of dated commitments (appointments, deadlines,
-- reminders, recurring patterns) extracted from note bodies/attachments — or
-- entered by hand. The note stays the source of truth (like note_analysis /
-- lab_results); these rows are a re-derivable sidecar, never authoritative.
-- identity_key is a deterministic dedup hash with a partial-unique index, so a
-- re-extraction upserts in place instead of duplicating (NULL opts out / manual).
CREATE TABLE IF NOT EXISTS calendar_events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  note_id        INTEGER REFERENCES notes(id) ON DELETE CASCADE,        -- provenance
  attachment_id  INTEGER REFERENCES attachments(id) ON DELETE SET NULL, -- if from a file
  title          TEXT NOT NULL,                  -- "Dentist — Dr. Lee", "Mortgage due"
  detail         TEXT,                           -- free-text context
  kind           TEXT NOT NULL DEFAULT 'event'   -- appointment|deadline|reminder|event|recurring
                   CHECK (kind IN ('appointment','deadline','reminder','event','recurring')),
  starts_at      TEXT,                           -- ISO 8601 (UTC) or date-only; NULL = undated TODO
  ends_at        TEXT,
  all_day        INTEGER NOT NULL DEFAULT 0,
  tz             TEXT,                            -- IANA tz the local time was authored in (display)
  rrule          TEXT,                            -- iCal RRULE (RFC 5545) for recurrence; NULL = one-off
  rdate_json     TEXT NOT NULL DEFAULT '[]',      -- explicit extra dates
  exdate_json    TEXT NOT NULL DEFAULT '[]',      -- exceptions (skipped instances)
  location_label TEXT, lat REAL, lon REAL,        -- optional; can resolve to a place
  place_id       INTEGER REFERENCES places(id) ON DELETE SET NULL,
  person_id      INTEGER REFERENCES people(id) ON DELETE SET NULL,      -- whose event (reuse people)
  entity_id      INTEGER REFERENCES entities(id) ON DELETE SET NULL,    -- the doctor/org/etc.
  status         TEXT NOT NULL DEFAULT 'confirmed'
                   CHECK (status IN ('confirmed','tentative','cancelled','done')),
  identity_key   TEXT,                            -- dedup hash; NULL = manual, never dedup'd
  source         TEXT NOT NULL DEFAULT 'extracted', -- 'extracted'|'manual'|'workflow'
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_calevents_identity
  ON calendar_events(identity_key) WHERE identity_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_calevents_starts ON calendar_events(starts_at);
CREATE INDEX IF NOT EXISTS idx_calevents_note   ON calendar_events(note_id);
CREATE INDEX IF NOT EXISTS idx_calevents_kind   ON calendar_events(kind, starts_at);
```

Conventions mirrored deliberately: `identity_key` + partial-unique index for
idempotent re-extraction (`schema.sql:699,747`), `note_id`/`attachment_id`
provenance on every row (`schema.sql:725-727`), `person_id` reuse of the existing
`people` table (`schema.sql:642`), `place_id`/`entity_id` reuse so a Calendar
links into the graph rather than re-inventing.

### 1.2 Recurrence: store the rule, materialize on demand

Store `rrule` as an iCal RFC 5545 string (`FREQ=WEEKLY;BYDAY=TH`). Do **not**
pre-explode every future instance into rows — that's unbounded and goes stale.
Instead a query-time/materialization helper expands the rule over a window. This
mirrors how `trips` are a *derived cache* recomputed from a cursor
(`schema.sql:541` `trip_cursor`) rather than authored once.

Detection of recurrence reuses the **existing** chatter-clustering machinery:
`promote_recurrences` already finds "a thing logged across >= N distinct days"
(`actions/promote_recurrences.yaml`, `cluster_chatter` primitive). v1 adds a
branch: when such a cluster is *date-regular*, emit a `kind='recurring'`
calendar_events row with a best-fit `rrule` instead of (or in addition to) a
kb/Patterns article.

### 1.3 The read view (the query API)

Mirror the `v_lab_trend` / `v_encounter_timeline` view pattern (`schema.sql:860,881`)
so the SQL console and Research-mode `query_sql` answer temporal questions with
one obvious SELECT:

```sql
CREATE VIEW IF NOT EXISTS v_upcoming AS
  SELECT e.id, e.title, e.kind, e.starts_at, e.ends_at, e.all_day,
         e.status, e.location_label, e.rrule,
         p.name AS person, n.title AS note_title, n.slug AS note_slug
  FROM calendar_events e
  LEFT JOIN people p ON p.id = e.person_id
  LEFT JOIN notes  n ON n.id = e.note_id
  WHERE e.status NOT IN ('cancelled','done')
    AND (e.starts_at IS NULL OR e.starts_at >= datetime('now'))
  ORDER BY e.starts_at;
```

A companion `v_event_history` (past, ordered desc) answers "when did I last..."
and "how often."

---

## 2. Population (three paths, all in keeping with existing patterns)

### 2.1 Extraction action + workflow (primary)

A declarative action `actions/extract_events.yaml` + workflow
`workflows/extract-events.yaml`, modeled on `analyze-notes` and the lab-ingest
flow. Steps (all using existing or thin-new `pipeline.py` `_PRIMITIVES`):

1. `query_notes` for entries changed since the last run (a `meta` watermark,
   like every scheduled action).
2. For each, read `note_analysis.dates_json` (already computed —
   `note_analysis.py:149`) + the note body; an LLM step classifies each dated
   mention into `{title, kind, starts_at, ends_at, rrule?, status}` JSON.
3. A new primitive `upsert_calendar_events` writes rows keyed by `identity_key =
   sha256(note_id | normalized_title | starts_at)` so re-runs upsert, never
   duplicate (the `lab_results` discipline).
4. Default **stages** the proposals (no destructive auto-apply — the project's
   firm rule, README "No destructive auto-apply"); `auto_apply` config writes
   directly for the confident date-only cases.

Because rows carry `note_id`, deleting/editing a note re-derives or cascades
(`ON DELETE CASCADE`), so the projection self-heals.

### 2.2 Manual entry (escape hatch)

A `POST /api/calendar` create path (`source='manual'`). To honor the
source-of-truth boundary, manual creation **also appends a dated line to a note**
(a "Calendar" or daily-log note) via the existing `append_to_note` primitive, so
the durable record still lives in a note and the manual row is just its index
entry. Manual rows set `identity_key = NULL` so extraction never clobbers them.

### 2.3 Recurrence promotion (from existing detector)

Extend `promote_recurrences` (§1.2) to emit `kind='recurring'` rows. No new
clustering code — just a new sink.

---

## 3. Reminders — Review cards **and** Web Push (the v1 choice)

A scheduled workflow `workflows/upcoming-reminders.yaml` runs daily (cron, server
TZ — already supported, README "Workflows"). It:

1. Materializes recurring rules into concrete instances for the lookahead window.
2. For events crossing a lead-time threshold (e.g. within 24–48 h), posts a
   **Review card** through the existing `create_review` primitive /
   `review_items` inbox with a `link_slug` to the source note (`schema.sql:264`,
   the count-badge inbox the README documents).
3. **Additionally** fires a **Web Push** notification for time-sensitive
   `kind IN ('appointment','deadline')` via the existing `push` service /
   `push_subscriptions` (`schema.sql:461`, `server/app/services/push.py`,
   `routers/push.py`). A `meta`/`location_fired`-style dedup table
   (`calendar_fired`) ensures one notification per instance, never a re-nag on
   re-run (the `location_fired` pattern, `schema.sql:630`).

No new notification infrastructure — both channels already ship.

---

## 4. Surfaces

- **SQL console / Research mode** — works day one via `v_upcoming` /
  `v_event_history` (read-only `query_sql` already allows SELECT/WITH).
- **Research-mode tools** (optional, phase 2) — `list_upcoming` and
  `event_history`, thin read-only tools mirroring the lab tools
  (`prompts.yaml` `list_abnormal_labs` / `lab_stat` shape) so the AI answers
  "what's on my calendar this week" conversationally and cites the source note.
- **Calendar tab** (phase 3, `web/`) — a month/agenda view under Advanced
  (alongside Browse · Automate · Data · Review), each item linking to its note.
  Out of scope for the doc-only first pass.
- **Search** — automatic: rows link to notes, which are already in FTS5 +
  semantic search.

---

## 5. Phasing

- **Phase 0 (this doc):** approve the schema + boundary rules.
- **Phase 1:** migration adds `calendar_events` + `calendar_fired` + views;
  `extract_events` action/workflow (staged); `upsert_calendar_events` primitive.
  Queryable via SQL/Research immediately. *(This was the "Schema + extraction
  only" option.)*
- **Phase 2:** `list_upcoming` / `event_history` Research tools; recurrence
  promotion branch in `promote_recurrences`.
- **Phase 3:** `upcoming-reminders` workflow (Review cards + Web Push); manual
  entry API + the source-note append.
- **Phase 4:** Calendar tab in the PWA.

---

## 6. Open questions for review

1. **Recurrence engine** — pull in a small `dateutil.rrule` dependency for
   expansion, or hand-roll the limited subset (daily/weekly/monthly) we actually
   emit? (`dateutil` is heavier but correct.)
2. **All-day & timezone** — store `starts_at` as date-only strings for all-day
   events (mixing date and datetime in one column, as `lab_results.collected_at`
   already does), or split a separate `date`/`time`? The medical tables chose the
   single-column approach; matching it is simpler.
3. **Manual-vs-extracted boundary** — is the "manual create also appends to a
   note" rule (§2.2) worth the friction, or should manual rows be allowed to
   stand alone (accepting the slight source-of-truth drift)?
4. **Lead time / quiet hours** — should reminder lead-time and a no-push window
   be workflow config (per the editable-workflow pattern) or fixed?
5. **Scope of the recurrence branch** — should detecting a recurrence *replace*
   the kb/Patterns article it currently writes, or produce both (article for the
   narrative, calendar row for the schedule)?
