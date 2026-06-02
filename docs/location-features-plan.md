# Location features — implementation plan (assisted tools + triggers)

Build on the new `locations` trail table: let **assisted mode answer location-by-time
questions**, and add **location/time triggers**. Two cruxes from the brainstorm carry
in: **(1)** coords→place labeling must be private (match the user's own places, no
third party), and **(2)** the trail is sparse (hourly / 100 m) so lean on dwell/away
timescales, not instantaneous geofencing.

## 1. Places model (the shared dependency)

Tools *and* triggers need named geofences with a **radius** — and `notes.lat/lon`
has no radius. Add a tiny table (migration 23):

```sql
CREATE TABLE places (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,            -- "Home", "Gym"
  lat REAL NOT NULL, lon REAL NOT NULL,
  radius_m INTEGER NOT NULL DEFAULT 150,
  note_slug TEXT,               -- optional link to a note
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```
- Owner CRUD: `GET/POST/DELETE /api/places` (bearer). A small "Places" panel on the
  Map tool (drop a pin / set radius) — reuse the map we built.
- `geotrail.label_point(conn, lat, lon)`: nearest place whose circle contains the
  point → its name; else nearest coord-**note** within a loose cap → its title; else
  `None`. **Private, no external geocoder.**

## 2. Service layer — `server/app/services/geotrail.py` (pure, unit-tested)

All geo math server-side (the LLM never does trig on raw rows):
- `fixes(conn, since, until) -> list` — range query (reuse the `/api/locations` filter).
- `nearest_fix(conn, when_iso) -> row|None` — closest `recorded_at`; return the gap so
  the tool can say "(±37 min — no fix closer)".
- `dwell_minutes(conn, lat, lon, radius_m, since, until) -> float` — sum the gap to the
  next fix for each consecutive in-radius fix.
- `stay_points(conn, since, until, radius_m, min_min) -> list` — cluster consecutive
  fixes within radius held ≥ min_min; label each via `label_point`.
- `distance_km(conn, since, until) -> float` — sum haversine along the ordered trail.
- Reuse `geo.haversine_km`. Everything keys off `recorded_at` (UTC strings).

## 3. Assisted-mode tools (assisted **and** research; read-only)

Registered the usual way (`_TOOL_SCHEMAS` + `_run_tool` dispatch + `prompts.yaml`
`modes.{assisted,research}.tools` + `tools.<name>` description; the
`test_agent_config_complete_and_valid` pin must stay green):

| Tool | Returns |
|------|---------|
| `where_was_i(when)` | label + time of the nearest fix (+ the gap) |
| `time_at_place(place, since?, until?)` | dwell minutes near a place |
| `places_visited(since?, until?)` | labeled stay-points with arrive/leave/duration |
| `distance_traveled(since?, until?)` | km along the trail |
| `trail_summary(since?, until?)` | narrative: places, durations, distance |

**Egress mitigation:** tools return **labels + durations**, never raw coordinate
dumps, so what reaches the Claude API is "Gym · 45 min", not lat/lon lists. Add a
`location_tools_enabled` meta flag (default on) so the owner can hard-disable sending
any location to the LLM.

## 4. Reverse geocoding

- **v1:** `label_point` (places + coord-notes) only — private, zero deps.
- **Deferred/opt-in:** a server-proxied Nominatim `reverse_geocode(lat,lon)` with
  on-disk cache + a proper User-Agent + ≤1 req/s, gated behind a meta flag. External +
  a privacy leak (your server's IP queries OSM), so off by default.

## 5. Triggers

New event names on the existing `event` trigger type, fired via `workflows.fire_event`:
`location:arrived`, `location:left`, `location:dwell`, `location:away`,
`location:new_place`. The action (a normal recipe) gets `{place, lat, lon, minutes}`
and can log/notify (reuse Web Push).

**Where evaluation happens:**
- **Ingest-time** (in `POST /api/locations`, after a point is *kept*): an evaluator
  reads active `event` workflows whose `event` is a `location:*` and whose config
  names a place; compares the **previous kept fix** vs the new one to detect
  enter/leave/dwell. Needs per-(workflow,place) state → a `location_state` table
  (`workflow_id, place_id, inside INT, since TEXT`).
- **Scheduler** (`run_due_scheduled`): `location:away` ("away from Home > 12 h") is a
  time comparison against the latest fix — no per-fix state, evaluated periodically.

**Debounce/hysteresis:** enter at `radius`, leave at `radius × 1.3` (or require 2
consecutive) so an edge fix doesn't flap.

**Async:** `fire_event` must NOT block the ingest response — dispatch matched
workflows to the existing background runner (don't run actions inline on the POST).

## Build order (security/value-first)
1. `places` table + CRUD + `geotrail.py` + **unit tests** (dwell, stay-points,
   distance, label_point) — no LLM, no triggers yet.
2. The 5 assisted tools + prompts wiring + config-pin test.
3. `location:dwell` + `location:away` triggers (the reliable ones) + `location_state`
   + async dispatch + a push.
4. `location:arrived`/`left`/`new_place` (sampling-limited; ship after the reliable ones).
5. Places UI on the Map tool; optional Nominatim proxy behind a flag.

## Gauntlet / risks
- **Sparse sampling:** enter/leave coarse/late/missed; `nearest_fix` interpolates —
  answers must hedge ("around"). Dwell/away are reliable. (Ship those first.)
- **LLM egress:** location → Claude; mitigated by labels-not-coords + a kill flag.
- **Trigger state + flapping:** needs the `location_state` table + hysteresis; multi-
  source (PWA + watch) interleave into one trail (state is global — fine).
- **Ingest latency:** evaluation + dispatch must be async/cheap so posting a fix stays fast.
- **`query_sql` overlap:** the architect can already read `locations`; these tools earn
  their keep only by doing the haversine/dwell/clustering it can't reliably do on rows.
- **Stay-point tuning:** radius/min-time are fiddly; expose sane defaults, allow override.

## Open decisions
1. **Places:** new `places` table (recommended) vs. reuse coord-notes + per-trigger radius.
2. **Reverse geocoding:** private-only v1 (recommended) vs. include the Nominatim proxy now.
3. **Trigger scope:** ship dwell+away first (recommended) vs. all five at once.
4. **Egress:** ship the `location_tools_enabled` kill-switch in v1? (recommended yes.)

---

## v2 — locked after hostile review (SHIPPED)

> Status: implemented. Migration 23 (`places`/`location_state`/`location_fired`),
> `geotrail.py`, the 5 assisted+research tools, places CRUD + Map-tool Places panel,
> ingest-time `location_state` refresh, the scheduler `evaluate_location_triggers`
> evaluator with `location_fired` dedup, and the `notify` primitive +
> `location_notify` action are all in.


**Decisions:** build **everything** (tools + away/dwell + arrived/left/new_place); **egress
left open** (no `_NON_CONTENT_TABLES` change, no kill-flag — `query_sql` may read raw coords;
accepted).

**Corrected trigger architecture (fixes C1/C2):** `fire_event` runs workflows **inline on the
caller's connection** — so the ingest path must NOT fire actions. Instead:
- **Ingest** (`POST /api/locations`, after a kept fix): only update **`location_state`** per
  place (cheap, no actions): `inside`, `since`, `last_inside_at`, `last_fix_at`. No `fire_event`.
- **Scheduler** (`evaluate_location_triggers(conn)` called from the scheduler loop on its own
  connection): read active `event` workflows with a `location:*` event, resolve the configured
  place, evaluate dwell/away/arrived/left/new_place against `location_state`, dedup via
  **`location_fired(workflow_id, kind, marker)`**, and run the matched workflow **directly**
  (`run_workflow(conn, wf, ctx)`) — NOT `fire_event` (which would broadcast to every workflow
  sharing the event name regardless of place). LLM-capable actions thus run on the scheduler
  thread where latency is fine.

**Schema (migration 23, bump SCHEMA_VERSION→23): three tables** — `places`, `location_state`
(PK `place_id`, physical truth + `last_inside_at`), `location_fired` (per-workflow dedup).

**Correctness fixes baked in:**
- **Dwell (H2):** split each inter-fix gap half to each endpoint, **cap per-gap at 90 min**
  (lost-trail), define first/last-fix handling explicitly.
- **Timezone (H3):** tools take **tz-aware/UTC ISO bounds** that the LLM computes from its
  app_tz grounding; the tool converts to the stored UTC format before comparing.
- **Distance (M6):** drop segments shorter than `max(accuracy_m, 30 m)` to kill GPS jitter.
- **Labeling (M2/M3):** places win; coord-note fallback capped at 150 m; no match → "an
  unlabeled spot" (never raw coords leaked through a tool).
- **Notify (M1):** add a `notify(title, body, url)` push helper + a `notify` pipeline primitive
  so a trigger's action can actually push.

**Build order:** (1) schema; (2) `geotrail.py` + tests; (3) places CRUD + 5 tools + prompts
wiring + config-pin test; (4) ingest `location_state` update; (5) scheduler evaluator +
`location_fired` + `notify` primitive + tests; (6) Map-tool places UI.
