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
drift toward "the place you edit appointments directly." **v1 closes this hole by
design: the calendar UI never writes the sidecar directly — every create/edit is a
NOTE write, and the sidecar is re-derived from notes by a consolidation pass.**

Concretely (the owner-confirmed model):

- **Creating** an event from the UI writes a **new dated/daily note** (or appends
  a structured line to today's daily note) — the durable record lives in a note,
  exactly like the existing Daily Log flow.
- **Changing/cancelling** an event is one of two note operations, never a direct
  row edit:
  1. **Edit the original note** (versioned like every edit), or
  2. **Write a superseding note** — "this replaces the dentist appt on the 14th →
     moved to the 21st" — that points back at the original.
- A **consolidation pass** then re-derives the calendar from notes: it upserts
  the changed rows and retires the superseded ones. This is the same
  "supersede stale facts + consolidate" discipline the KB maintenance already
  follows (`docs/kb-maintenance-redesign.md`), and the same Daily Log → Daily
  Summaries shape the README documents.

So **every** `calendar_events` row is derived, carries `note_id` provenance, and
is re-derivable — there is no special "manually authored, do not re-derive"
category to fight consolidation. `source` records *how the originating note line
was authored* (`extracted` from prose vs `manual` from the calendar quick-add UI),
but the row is a projection either way.

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
-- re-extraction upserts in place instead of duplicating. EVERY row is derived
-- from a note (the calendar UI writes notes, never these rows directly — §2.2).
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
  supersedes_note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL, -- a later note replaced/cancelled this (see §2.4)
  identity_key   TEXT,                            -- dedup hash; EVERY row has one (all rows are derived)
  source         TEXT NOT NULL DEFAULT 'extracted', -- how the source note line was authored: 'extracted'|'manual'|'workflow'
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

### 2.2 Manual entry & edits — ALWAYS via a note (no direct row writes)

The owner-confirmed rule: **the calendar UI never writes `calendar_events`
directly.** Every create/edit is a note write, and the same extraction pass
(§2.1) derives the row. This keeps notes the single source of truth with zero
special-casing.

- **Create** — the "add to calendar" UI writes a **new dated note** (or appends a
  structured line to today's daily note) via the existing `write_note` /
  `append_to_note` primitives. The quick-add form is just a convenient way to
  author a well-formed dated note line; the extractor turns it into a row on the
  next consolidation tick (or synchronously after the write).
- **Change/cancel** — never an in-place row edit. Two note operations only:
  1. **edit the original note** (it's versioned like any edit, and re-extraction
     upserts the changed row), or
  2. **write a superseding note** that references the original — see §2.4.

`source` on the resulting row is `manual` when the originating note line came from
the quick-add UI, `extracted` when it came from free prose — purely informational;
both are derived rows with `note_id` provenance and a real `identity_key`.

### 2.3 Recurrence promotion (from existing detector)

Extend `promote_recurrences` (§1.2) to emit `kind='recurring'` rows. No new
clustering code — just a new sink.

### 2.4 Supersession & consolidation

Because edits are note writes, the projection needs to know when a newer note
**replaces/cancels** an event a prior note created. This is the same problem KB
maintenance already solves by "superseding stale facts"
(`docs/kb-maintenance-redesign.md`). Mechanism:

- A superseding note carries an explicit back-reference to what it replaces:
  - from the UI **reschedule/cancel** action, the form pre-fills a marker (a
    `[[wiki-link]]` to the original note plus the original event's date), so the
    edge is unambiguous; and/or
  - in free prose, the consolidation LLM step recognizes "moved to / cancelled /
    rescheduled" language pointing at a prior event.
- The **consolidation pass** (a step in `extract_events`, or its own
  `consolidate_calendar` action on a watermark) then: writes the new/updated row,
  and sets the prior row's `status` to `cancelled` (or `done`) rather than
  deleting it — so history (`v_event_history`, "it was originally the 14th") is
  preserved, mirroring how `trips` snapshots and `note_versions` never lose the
  past.

Schema support: add `supersedes_note_id INTEGER REFERENCES notes(id)` to
`calendar_events` (the note that this row was superseded *by* is found by walking
forward; the simplest stored edge is "this note supersedes that note").

**Identity model — DECIDED (a)+(b):**

- **(a) Structured edge when the UI provides it.** The reschedule/cancel action
  pre-fills the superseding note with a precise marker — a `[[wiki-link]]` to the
  original note plus the original event's date — so consolidation matches the
  prior row unambiguously and sets its `status`.
- **(b) LLM best-effort otherwise.** For free-prose edits ("pushed the dentist to
  Friday") with no marker, the consolidation LLM step matches against recent
  open events by title/person/date proximity. Lower-confidence matches stage for
  review rather than auto-cancelling.

A fully-deterministic logical `series_key` (option c) is **deferred** to a later
hardening pass — it's the most robust but hard to compute reliably from prose, and
(a)+(b) covers the real cases for v1.

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
- **Calendar tab** (phase 4, `web/`) — a month/agenda view under Advanced
  (alongside Browse · Automate · Data · Review), each item linking to its note.
  Its quick-add / reschedule / cancel controls **write notes** (§2.2), not rows.
  Out of scope for the doc-only first pass.
- **Search** — automatic: rows link to notes, which are already in FTS5 +
  semantic search.

---

## 5. Phasing

- **Phase 0 (this doc):** approve the schema + boundary rules.
- **Phase 1:** migration adds `calendar_events` (+ `supersedes_note_id`) +
  `calendar_fired` + views; `extract_events` action/workflow (staged);
  `upsert_calendar_events` + consolidation/supersession (§2.4) primitives.
  Queryable via SQL/Research immediately. *(This was the "Schema + extraction
  only" option.)*
- **Phase 2:** `list_upcoming` / `event_history` Research tools; recurrence
  promotion branch in `promote_recurrences`.
- **Phase 3:** `upcoming-reminders` workflow (Review cards + Web Push); calendar
  **quick-add** UI that writes a dated note (never the row) + the
  reschedule/cancel action that writes a superseding note (§2.2/§2.4).
- **Phase 4:** Calendar tab in the PWA (month/agenda view).

---

## 6. Open questions for review

1. **Recurrence engine** — pull in a small `dateutil.rrule` dependency for
   expansion, or hand-roll the limited subset (daily/weekly/monthly) we actually
   emit? (`dateutil` is heavier but correct.)
2. **All-day & timezone** — store `starts_at` as date-only strings for all-day
   events (mixing date and datetime in one column, as `lab_results.collected_at`
   already does), or split a separate `date`/`time`? The medical tables chose the
   single-column approach; matching it is simpler.
3. **Supersession identity model — DECIDED (a)+(b)** (§2.4). UI-driven
   reschedule/cancel writes a structured `[[wiki-link]]` + date marker for an
   unambiguous match; free-prose edits fall back to LLM best-effort matching
   (low-confidence matches stage for review). A deterministic `series_key`
   (option c) is deferred. The source-of-truth question is also settled (§0/§2.2:
   the UI always writes notes; the sidecar is re-derived).
4. **Lead time / quiet hours** — should reminder lead-time and a no-push window
   be workflow config (per the editable-workflow pattern) or fixed?
5. **Scope of the recurrence branch** — should detecting a recurrence *replace*
   the kb/Patterns article it currently writes, or produce both (article for the
   narrative, calendar row for the schedule)?

Open questions 1 & 2 are resolved by the Phase 1 plan below (1: use `dateutil` —
it is already a transitive dependency via `croniter`, so no new dep; 2: single
`starts_at` column + an explicit `all_day` bit).

---

## 7. Phase 1 — consolidated implementation plan (best-of-N + adjudication)

Two independent plans (a convention-follower and an adversarial simplicity pass)
were produced and reconciled. They converged on the major calls and split on
three; the adjudicated decisions below are what gets built. Several **correct the
draft schema in §1.1** — those corrections are authoritative for Phase 1.

### Adjudicated decisions

- **Auto-write, not stage.** Extraction writes `calendar_events` rows directly,
  like `note_analysis`/`entities` (a re-derivable sidecar that never mutates a
  note and carries no PHI). The "no destructive auto-apply" rule governs *note/KB*
  writes, which this is not. The ONLY staged surface is a low-confidence
  free-prose supersession match (it could retire a live row) — that posts a Review
  card instead of auto-cancelling. **Corrects §2.1 step 4 / §5 ("staged").**
- **`identity_key = sha256(note_id │ normalized_title │ kind │ seq)` — the date is
  NOT in the key.** Keying on the date would make editing a note's date create a
  duplicate instead of moving the event; the date is a mutable attribute the
  upsert UPDATEs in place. `seq` (0-based, by document order within the note)
  disambiguates two genuinely-distinct same-title/kind events in one note so the
  second can't silently overwrite the first. **Corrects §2.1 step 3.**
- **Supersession edge = a `calendar_supersedes` table** keyed by the STABLE
  `identity_key` (`old_identity_key`, `new_identity_key`, `superseded_by_note_id`,
  `confidence`). *(Adversarial review reversed an earlier "column on the row" call:
  a column gets clobbered when the ORIGINAL note — which still mentions the event —
  is re-extracted and its row rewritten in place. The edge must live outside the
  churning row.)* The views exclude rows whose `identity_key` appears as an
  `old_identity_key`. Sweeping/deleting an event purges its edges (so a removed-then-
  re-added date can't be resurrected by a stale edge), and `consolidate` RECONCILES
  a note's structured edges from its current markers (a removed marker retracts its
  edge). **Replaces §1.1/§2.4's `supersedes_note_id`.**
- **Both supersession paths shipped.** (a) structured `supersedes/cancels [[Note]]
  DATE` markers (deterministic, single-line-bounded regex; replacement matched by
  title affinity, not "latest date in the note"). (b) free-prose: `propose_supersessions`
  asks the LLM whether a reschedule/cancel note matches a live event — HIGH confidence
  records an `'llm'` edge, LOW posts a Review card (never auto-applied); no-op without
  an LLM key.
- **Watermark = a composite `"<updated_at>|<id>"` cursor.** A bare `max(updated_at)`
  starves notes sharing a timestamp once `batch_limit` truncates them; paging by
  `(updated_at, id)` guarantees forward progress. `pending_notes` also surfaces notes
  that carry a supersession marker (so a date-less pure-cancellation note is scanned)
  or already have calendar rows (so a date removed from a note sweeps its orphans).
- **rrule via `dateutil.rrule`** (already present transitively through `croniter`;
  added explicitly to requirements). Correct BYDAY/COUNT/UNTIL/DST handling
  without re-implementing RFC 5545. **Resolves open question 1.**
- **all-day:** single `starts_at` (date-or-datetime string, like
  `lab_results.collected_at`) + an explicit `all_day` bit; `tz` is display-only,
  no UTC conversion in v1. **Resolves open question 2.**
- **`calendar_fired`** (reminder dedup, created now, used in Phase 3) mirrors
  `location_fired`: `(workflow_id, kind, marker)` PK, `marker = identity_key│instance_date`
  so recurring instances dedup individually.
- **Views** (`v_upcoming`, `v_event_history`) live in both schema.sql and the
  factored `_CALENDAR_SCHEMA_SQL` migration constant, matching `_MEDICAL_SCHEMA_SQL`.

### File-by-file (ordered)

1. `server/app/schema.sql` — add a "Calendar (temporal projection)" section:
   `calendar_events` (corrected columns above), `calendar_fired`, `v_upcoming`,
   `v_event_history`.
2. `server/app/db.py` — `SCHEMA_VERSION 44 → 45`; add `_CALENDAR_SCHEMA_SQL`
   (identical to the schema.sql section); `if current < 45:` migration runs it.
3. `server/requirements.txt` — add `python-dateutil` (make the direct use explicit).
4. `server/app/services/calendar.py` (new) — deterministic core: `normalize_title`,
   `identity_key`, `upsert_events` (ON CONFLICT upsert + per-note deletion sweep of
   live rows), `apply_supersession` (structured marker → set superseded), 
   `parse_supersession_marker` (regex over `[[link]]` + ISO date), `consolidate`
   (idempotent re-assert), `expand_rrule` (dateutil), `classify_dates` (LLM front
   end, stubbable), `pending_ids`.
5. `server/app/services/pipeline.py` — thin primitives `calendar_pending`,
   `extract_events`, `upsert_calendar_events`, `consolidate_calendar` + their
   `_PRIMITIVE_META` entries (the pinned test must stay green).
6. `actions/extract_events.yaml` + `workflows/extract-events.yaml` — scheduled
   `cron: "0 2 * * *"` (after note-analysis at 1am, so `dates_json` is fresh).
7. `server/tests/test_calendar.py` (new) — migration idempotency/upgrade,
   identity-key MOVE-not-duplicate, upsert idempotency, deletion sweep,
   structured supersession + the "what replaced X" SELECT, rrule expansion,
   the views' filters, full `extract_events` run with a stubbed LLM, and the
   `_PRIMITIVE_META` pin (kept green).
