# JBrain Backend HTTP API Surface Inventory

**Document Date:** 2026-06-08  
**Scope:** All routes in `/server/app/routers/` + main.py router wiring  
**Total Endpoints:** 115+ (counted by unique route + method)

---

## Auth Router (`auth_router.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/auth/info` | GET | Brain name for pre-auth key-entry screen | Public (no auth); reveals brain_name only, not version | Low |
| `/api/auth/verify` | GET | Confirm pasted access key is valid; return version + capabilities | Auth-gated (CurrentUser); returns version, LLM status, VAPID key, owner name, TZ | Med |

---

## Notes Router (`notes.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/notes` | GET | List all notes (paginated, filterable by q/kind) | Hides protected (underscore prefix) + redirects by default; limit 200 | Low |
| `/api/notes/located` | GET | Notes with geolocation (map pins) | Filters by since/until; truncates to 5000; no protection secrets | Low |
| `/api/notes/{slug}` | GET | Read full note with backlinks, tags, redirect resolution | Fetches versions history; resolves redirect chains; returns all fields | Low |
| `/api/notes/{slug}/preview` | GET | Tiny title + excerpt for hover citations | Uses AI gist if available, else lead text; 280 char limit | Low |
| `/api/notes/{slug}/analysis` | GET | Read-only AI analysis sidecar (gist, entities, domain) | Never mutates; empty {} if not computed | Low |
| `/api/notes/{slug}/analysis` | POST | Force-recompute analysis (ignore cache hash) | Runs title check (renames if undated); rebuilds entity index; commits | High |
| `/api/notes/{slug}/talk` | GET | Article maintenance memory (decisions, conflicts, directives) | Per-article talk items; no auth on read | Low |
| `/api/notes/{slug}/talk` | POST | Add owner note/directive/question; promote correction to source note | kind ∈ {note, question, directive, correction}; creates truth layer | High |
| `/api/notes/{slug}/talk/{talk_id}/reply` | POST | Reply to talk item (owner↔AI maintenance conversation) | Folded into next maintenance pass | Med |
| `/api/notes/{slug}/talk/{talk_id}/dismiss` | POST | Owner dismissal of talk item (distinct from resolution) | Blocked on corrections (refuse to hide truth notes) | Med |
| `/api/notes/{slug}/talk/maintain-now` | POST | Run surgical maintenance pass on THIS article immediately | Under KB lock; consumes open items + owner replies | High |
| `/api/notes/kb/dead-links` | GET | KB health: dangling cross-links | Lists unresolved [[target]] references | Low |
| `/api/notes` | POST | Create/update note (upsert) | Fires entry_created hooks (auto-tag, etc.); commits | High |
| `/api/notes/{slug}` | PUT | Edit note + rename (id-targeted, preserves slug chain) | Target by slug; upsert by id; preserves kind; commits | High |
| `/api/notes/{slug}/tags` | PUT | Replace note's tags (direct owner edit) | PATCH semantics within tag set; commits | Med |
| `/api/notes/{slug}/flags` | PUT | Set kb_ingest / tool_access flags | Validates coherent states (kb_ingest=1 requires tool_access=1); clears markers on 0→1; commits | High |
| `/api/notes/{slug}/versions` | GET | Timeline of authored states (newest first) | source ∈ {user, architect, import, kb, rename}; includes conversation_id, size | Low |
| `/api/notes/{slug}/restore` | POST | Restore a historical version | target by version_id; rewrites title if present; commits | High |
| `/api/notes/{slug}` | DELETE | Soft-delete note (deleted_at timestamp) | Refuses if DB locked (503 + message); surface real error, not opaque 500 | High |
| `/api/notes/entry` | POST | Dictation capture (watch/phone) | LOOSE auth: full key OR per-person location key; auto-tags; fired after commit; captures lat/lon | High |

**Notes on notes.py:**
- Entry router (entry_router) allows per-person location key scoping for family member dictation
- Captures root = medical | financial | notes; auto-generates dated/destination titles
- Conversation location is captured (lat/lon) and stamped on versions for context

---

## Chat Router (`chat.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/chat/conversations` | POST | Create new conversation | Inserts empty row; returns id | Low |
| `/api/chat/conversations` | GET | List conversations (limit 100, newest first) | No pagination control; shallow rows only (id, title, started_at) | Low |
| `/api/chat/conversations/{conversation_id}/messages` | GET | Message history with step counts | LEFT JOIN step count (cheap aggregate); no tool log details yet | Low |
| `/api/chat/messages/{message_id}/steps` | GET | Full raw tool-call history for one assistant reply | Lazily fetched when history panel opens; includes args_json, result_text, is_error, event_json | Med |
| `/api/chat/conversations/{conversation_id}/steps` | DELETE | Wipe stored tool-call history for conversation | Invoked on /clear; prevents log accumulation; commits | High |
| `/api/chat/conversations/{conversation_id}/message` | POST | Send message + stream SSE reply | mode ∈ {assisted, research} with fallback to read-only; fresh_context clears prior grounding; location optional; SSE keepalive 15s | High |

**Chat Behaviors:**
- Modes: `assisted` (Full Brain, write tools) vs `research` (read-only, deep opt-in budget)
- Retired modes (analyze → research) fold to read-only; unknown modes fail safe to read-only
- First message auto-titles conversation (first 60 chars of text)
- Streaming SSE with `: keepalive` comments to prevent proxy drops
- Conversation location (lat/lon/label) optionally captured for context

---

## Search Router (`search.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/search` | GET | Hybrid search (FTS5 keyword + semantic) over notes, attachments, entities | mode ∈ {hybrid, keyword, semantic, entities}; returns ranked results by bm25 + embedding distance; limit 20 | Med |

**Search Details:**
- FTS5 keyword search on notes + attachments (BM25 ranking, prefix queries)
- Semantic (vec_notes, embeddings model) with graceful degradation (swallow errors, fallback to keyword)
- Entity index search (canonical names + aliases) merged into same ranking
- Filters: notes.deleted_at IS NULL + attachments via note's deleted_at
- Results keyed by composite (note:id, att:id, entity:id) to dedupe hits

---

## Attachments Router (`attachments.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/notes/{slug}/attachments` | POST | Upload file (multipart); auto-analyze if enabled | Max 100 MB; enqueues image analysis, audio transcription, document text extraction; stamps mime, filename | High |
| `/api/attachments/{att_id}/analyze` | POST | Manual image/vision analysis (force rerun) | force flag bypasses "pending" gate; returns status immediately, work async | High |
| `/api/attachments/{att_id}/transcribe` | POST | Manual audio/video transcription | force flag bypasses "pending" gate; returns status immediately | High |
| `/api/attachments/{att_id}/analysis-status` | GET | Poll analysis/transcription progress | Returns status ∈ {pending, error, done, none}, detail, analyzed_at | Med |
| `/api/notes/{slug}/attachments` | GET | List attachments for a note | Returns id, filename, mime, byte_size, analysis_status | Low |
| `/api/attachments/{att_id}` | GET | Fetch metadata for one attachment | Returns filename, mime, content_text, byte_size, created_at | Low |
| `/api/attachments/{att_id}/media-url` | GET | Mint short-lived signed URL for browser inline streaming | Token-gated (not bearer); same-origin, no blob: needed | Low |
| `/api/attachments/{att_id}/stream` | GET | Range-capable media streaming (public, token-gated) | Supports HTTP 206 range requests; inlines image/audio/video; forces download for script-y types | Med |
| `/api/attachments/{att_id}/download` | GET | Force-download any attachment (no inline) | Neutralizes svg+xml, text/html, javascript MIMEs to application/octet-stream | Med |
| `/api/attachments/{att_id}` | DELETE | Delete attachment | Soft or hard delete; commits | High |

**Attachments Behaviors:**
- Upload auto-enrichment gated by "auto-analyze new notes" toggle
- analyze=false opt-out (e.g., for chat-uploaded attachments with no note context)
- Image/audio/video run async; documents extract text synchronously + re-run note analysis
- Media streaming uses signed per-attachment tokens (no bearer, no blob:) to preserve CSP
- All attacker-uploadable content forced to download (no XSS via SVG/HTML/JS MIME sniffing)

---

## Workflows Router (`workflows.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/workflows` | GET | List all workflows | Includes locked status (repo vs user-edited); order by name | Low |
| `/api/workflows/action-types` | GET | Catalog of action types + config-form schemas | Data-driven picker; forms schema for UI | Low |
| `/api/workflows/{wf_id}` | GET | Fetch one workflow | Full config, trigger, action; locked status | Low |
| `/api/workflows` | POST | Create new workflow | source='user', locked=1 (user-created are locked) | High |
| `/api/workflows/{wf_id}` | PUT | Update workflow | Sets locked=1, updated_at; full update (not patch) | High |
| `/api/workflows/{wf_id}/toggle` | POST | Toggle enabled flag | Sets locked=1; flips enabled bit | High |
| `/api/workflows/{wf_id}` | DELETE | Delete workflow | Soft or hard delete | High |
| `/api/workflows/{wf_id}/run` | POST | Start workflow immediately | Returns immediately; runs in background; poll /run-status for progress | High |
| `/api/workflows/{wf_id}/run-status` | GET | Latest run state (status, detail, live step trace) | status ∈ {running, ok, error, skipped, none}; step_since / now for elapsed time display | Med |
| `/api/workflows/sync` | POST | Re-ingest repo YAML workflows | Adds new, updates unlocked changed ones; returns synced count + full list | High |
| `/api/workflows/{wf_id}/reset` | POST | Unlock workflow so repo definition refreshes it | Clears locked flag; re-applies repo definition if present | High |
| `/api/workflows/{wf_id}/runs` | GET | Run history (limit 50, newest first) | Per-run: id, started_at, status, detail | Low |

**Workflows Details:**
- trigger_type ∈ {event, schedule}; action_type from catalog
- trigger_config / action_config are JSON; parsed on server
- Repo workflows (source='repo') are READ-ONLY; edits set source='user', locked=1
- Manual runs can take minutes (LLM calls); watch status endpoint for live progress
- Scheduling loop runs every 60s, location triggers evaluated off the scheduler thread

---

## Reviews Router (`reviews.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/reviews` | GET | List pending review items | status filter (pending/dismissed/etc.); limit 200, newest first | Low |
| `/api/reviews/count` | GET | Count of pending reviews | Returns {"pending": N} | Low |
| `/api/reviews/history` | GET | Dismissed reviews in last 24h | Limit 200; helps recover accidentally-dismissed items | Low |
| `/api/reviews` | POST | Manually create review item | title, message, optional link_title (resolved to slug); commits | Med |
| `/api/reviews/{review_id}/dismiss` | POST | Dismiss pending review | Sets status='dismissed', dismissed_at=now; commits | Med |
| `/api/reviews/{review_id}` | POST (entity_merge) | Approve/reject entity merge proposal | Requires current status='pending' (concurrency guard); triggers rebuild | High |

**Reviews Details:**
- kind ∈ {entity_merge, ...}; payload_json holds merge details
- Status states: pending → dismissed | approved | rejected
- Double-action protection: reject on status mismatch (409)
- Entity merge triggers coalesced background rebuild (UI polls /status to track)

---

## Staging Router (`staging.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/staging` | GET | List pending staged actions | Includes preview (before/after), payload, warnings (dead links, etc.); no pagination | Med |
| `/api/staging/{action_id}/preview` | GET | Diff preview for one staged action (read-only) | Mirrors apply logic without writing; signals stale if basis changed | Med |
| `/api/staging/{action_id}/apply` | POST | Apply staged action to wiki | Commits; records location (lat/lon) on versions; optimistic concurrency (basis hash guard) | High |
| `/api/staging/{action_id}/reject` | POST | Discard staged action | Deletes from queue; no undo | Med |
| `/api/staging/{action_id}/undo` | POST | Undo-ish: restore/reverse last apply | Inverse operations (delete_place, restore_note, etc.) | High |

**Staging Details:**
- Action types: CREATE, UPDATE, DELETE, DELETE_LIST, RENAME, LINK, LIST_REMOVE_ITEM, LIST_EDIT_ITEM, ADD_PLACE, EDIT_PLACE, SET_TAGS
- _basis: {note_id, content_hash} captures snapshot at propose time; refused on mismatch (409 "stale")
- Verify on apply: dead-link detection, fabrication checks (link target must exist)
- Staged CREATE is create-only (never clobber a colliding title)
- Inverse recorded for some ops (place delete → add_place undo)

---

## External Lookups Router (`external.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/external-lookups/{lookup_id}/approve` | POST | Approve proposed external lookup + run fetch NOW | Optional term edit before send; only approval moment sends data; caches result | High |
| `/api/external-lookups/{lookup_id}/deny` | POST | Decline proposed lookup (nothing sent) | Won't re-propose; commits | Low |

**External Lookups Details:**
- tool ∈ {medical_reference, drug_reference}
- Owner can edit term before approval (trim PHI)
- Fetch runs at approval (the only outbound moment)
- Result cached so next architect call returns it without re-fetching
- Drug → RxNorm + MedlinePlus Connect; topic → health-topics API

---

## Graph Router (`graph.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/graph` | GET | Knowledge graph (nodes + edges) for D3 viz | Nodes: id, title, slug, kind, in_degree + 1; edges: source/target note ids | Low |

**Graph Details:**
- Hides protected pages (underscore prefix) from nodes AND edges (no phantom nodes)
- Hides deleted notes and redirects
- Only edges between two live, non-hidden, non-redirect notes
- Self-loops excluded

---

## SQL Console Router (`sql_console.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/sql` | POST | Run ad-hoc SELECT query | Single SELECT/WITH only; max 200 rows; limit override param; read-only | High |

**SQL Details:**
- sqlsafe.run_select validates and restricts to SELECT / WITH (no INSERT/UPDATE/DELETE)
- Returns {columns, rows} tuple
- Errors surfaced to console UI (SQLite error strings)
- No pagination; defaults to 200 rows

---

## System Router (`system.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/system/version` | GET | Latest release tag vs this build; update available? | Fetches latest release from GitHub API (cached 1h); compares tag versions or commit SHAs | High |
| `/api/system/stats` | GET | Maintenance snapshot (disk, uptime, token usage) | DB footprint, attachment count, tile cache size, process uptime, daily/monthly token usage + estimated $, daily cost warning threshold | Med |
| `/api/system/update-log` | GET | Captured console output of latest deploy + status | Tails last N lines (default 800); status.json (state, phase, at); empty when nothing recorded | Low |
| `/api/system/update` | POST | Trigger self-update (non-destructive) | Runs JBRAIN_UPDATE_CMD if set, or writes flag for host helper; DB/secrets preserved; schema migrations run at next boot | High |
| `/api/system/export/original-notes` | GET | Download JSON of first user-authored version of each note | Excludes architect/kb/rename versions; JSON array [{title, content_md, created_at}]; downloadable file | Med |
| `/api/system/backup` | GET | Download consistent snapshot of entire database (.db file) | Uses backup_to_file() for atomic snapshot; downloadable DB file | High |

**System Details:**
- GitHub API fetch cached 1h (so stale updates are possible)
- Update is non-destructive (DB/volumes + .env preserved, migrations run on boot)
- Deploy log is a bind-mount shared with Caddy (survives api restart)
- Export original-notes picks earliest note_versions.id per note where source='user'

---

## System Status Router (`system_status.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/system/status` | GET | Soft-auth server health (public skeleton + authed full) | Public (missing/invalid key): {ok, brain, ts} only; Authed: + version, capabilities | Low |

**System Status Details:**
- Soft auth (no router-level CurrentUser); returns skeleton even with bad key
- PWA can probe "server reachable" vs "needs re-auth" without logout
- Public skeleton intentionally leaks ONLY what /api/health + /api/auth/info expose

---

## Prompts Router (`prompts_router.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/prompts` | GET | List all effective prompts (system + overrides) | Returns full catalog with current value (system default or user override) | Low |
| `/api/prompts/{key:path}` | PUT | Override a prompt value | key is hierarchical path (e.g., architect/research_budget); commits | High |
| `/api/prompts/{key:path}` | DELETE | Clear override, revert to system default | Removes user override; commits | High |

---

## Action Defs Router (`action_defs.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/action-defs/primitives` | GET | Primitive catalog (low-level steps: http_request, send_email, etc.) | Schema reference for recipe builders | Low |
| `/api/action-defs/validate` | POST | Parse YAML + validate recipe structure + return warnings | Detects missing steps, invalid syntax, refs to undefined action types | Med |
| `/api/action-defs/sync` | POST | Re-ingest repo action recipes (YAML) | Adds new, updates unlocked changed ones | High |
| `/api/action-defs` | GET | List all action recipes (type, source, locked, category, num_steps, summary) | Flattens YAML step tree; source ∈ {repo, user} | Low |
| `/api/action-defs/{type}` | GET | Fetch one action recipe | Returns parsed YAML tree + warnings + ref_count (workflows using this) | Low |
| `/api/action-defs` | POST | Create custom action recipe | recipe_yaml required; source='user', locked=1 | High |
| `/api/action-defs/{type}` | PUT | Update custom action recipe (repo recipes READ-ONLY) | Validates YAML; commits | High |
| `/api/action-defs/{type}` | DELETE | Delete custom action recipe | Refuses if workflows reference it (ref_count > 0) | High |

---

## Push Router (`push.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/push/subscribe` | POST | Register Web Push subscription | Stores endpoint, p256dh, auth keys, optional user agent; upserts (idempotent) | Low |
| `/api/push/unsubscribe` | POST | Deregister Web Push subscription | Removes by endpoint; idempotent | Low |
| `/api/push/test` | POST | Send test notification to all subscribed devices | Optional server-side delay (survives app close); returns status | Low |

---

## Medical Router (`medical.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/medical/destinations` | GET | Picklist of preconfigured medical capture folders | Defaults: [Admissions, Labs, Clinical Notes, Procedures, Medications, Imaging] | Low |
| `/api/medical/destinations` | PUT | Replace destination picklist | Sanitizes names (safe paths); case-insensitive dedup; max 50; commits | Med |
| `/api/medical/owner` | GET | Owner identity (name, DOB) used to flag wrong-patient lab uploads | Optional config; returns {} if not set | Low |
| `/api/medical/owner` | PUT | Set owner identity (name + normalized DOB) | Validates DOB format; commits | Med |
| `/api/medical/notes/{slug}/extract-labs` | POST | Stage lab-result PDFs for review (deterministic, no LLM) | Posts Review card; re-runs re-extract; nothing reaches lab_results until approved | High |
| `/api/medical/attachments/{attachment_id}/labs` | GET | Staged lab extraction (status + parsed results) | Drives Lab Import preview; returns {status, results} | Med |
| `/api/medical/attachments/{attachment_id}/labs/series` | GET | One analyte's trend from staged results (for preview plot) | Builds from unapproved results; optional unit filter | Low |
| `/api/medical/attachments/{attachment_id}/labs/approve` | POST | Ingest staged labs into persistent lab_results | Only moment unapproved data becomes canonical; commits | High |
| `/api/medical/attachments/{attachment_id}/labs/revoke` | POST | Discard approved labs (undo approve) | Re-opens approval window; commits | High |
| `/api/medical/attachments/{attachment_id}/labs/reanalyze` | POST | Re-parse PDF + re-stage (supersedes prior approval) | Like re-running image analysis; commits | High |
| `/api/medical/labs/pending` | GET | Attachments with extracted-but-unapproved labs | Drives "review imports" UI pointer; counts per-attachment | Low |
| `/api/medical/labs/analytes` | GET | Lab-chart picker feed (one per analyte with value) | Lists all analytes that have at least one result | Low |
| `/api/medical/labs/series` | GET | One analyte's full trend (points, reference bands, encounters) | For hand-rolled SVG chart; optional unit filter; time domain + value range | Low |

---

## Financial Router (`financial.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/financial/destinations` | GET | Picklist of preconfigured financial capture folders | Defaults: [Statements, Receipts, Invoices, Taxes, Accounts] | Low |
| `/api/financial/destinations` | PUT | Replace destination picklist | Sanitizes names (safe paths); case-insensitive dedup; max 50; commits | Med |

---

## Rebuild Router (`rebuild.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/kb/rebuild/start/{slug}` | POST | Stage 1: create run, stream gather agent's tool use → sources | SSE stream; returns run_id, slug, title, base_rev (hash) | High |
| `/api/kb/rebuild/{run_id}/regather` | POST | Stage 1 again: refine sources while keeping user's current picks | append=true; runs gather with optional hint; SSE stream | High |
| `/api/kb/rebuild/{run_id}/search` | GET | Note search for "add a source" picker on curate screen | Hybrid search limited to notes with kb_ingest=1; limit 8; returns id, title, date | Med |
| `/api/kb/rebuild/{run_id}/draft` | POST | Stage 2: write article from curated source ids | SSE stream; LLM call; may truncate if budget exceeded | High |
| `/api/kb/rebuild/{run_id}/redraft` | POST | Re-run drafting at larger user-approved budget after truncation | max_tokens override; SSE stream | High |
| `/api/kb/rebuild/{run_id}/guide` | POST | Steer in-flight draft with typed guidance | Runs only if live (status in {ready, guiding}); SSE stream | High |
| `/api/kb/rebuild/{run_id}/accept` | POST | Commit draft to wiki, optionally rename | rename_to must stay under kb/; optimistic concurrency (base hash guard); KB lock acquired | High |
| `/api/kb/rebuild/{run_id}/reject` | POST | Discard draft (no DB changes) | Run expires (in-memory only) | Low |

**Rebuild Details:**
- Session: in-memory (rebuild_runs), expires on close/refresh
- Basis guard: compare current note hash to run.base_hash at accept (refuse stale)
- Draft stages: gather (sources) → draft (write) → redraft (budget) → guide (refine) → accept (commit)
- KB lock held during accept (blocks maintenance)

---

## Entities Router (`entities.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/entities` | GET | List canonical entities (people/org/place/thing) | Optional type/q filters; limit 500; most-mentioned first | Low |
| `/api/entities/status` | GET | Entity rebuild progress indicator (rebuilding, status, generation, last_error) | Lets UI watch for fold materialization (poll generation) | Low |
| `/api/entities/merge` | POST | Durably merge source_id into into_id | Synchronous decision record + deferred background rebuild; returns survivor with rebuilding flag | High |
| `/api/entities/split` | POST | Forbid auto-union of pair (durable split decision) | Synchronous record + deferred rebuild; returns {ok, rebuilding} | High |
| `/api/entities/{entity_id}/aliases` | POST | Attach extra alias to entity | display required; triggers rebuild; returns updated entity detail | High |
| `/api/entities/{entity_id}/aliases/{alias_norm}` | DELETE | Remove user 'alias' decision | Triggers rebuild | High |
| `/api/entities/{entity_id}/decisions` | GET | Merge/split/alias decisions touching this entity's type | Ledger; immutable | Low |
| `/api/entities/resolve` | GET | Resolve name OR alias to canonical entity + its notes | Returns {resolved: entity_detail} or {resolved: null}; defined before /{id} so literal path wins | Low |
| `/api/entities/{entity_id}` | GET | Entity detail + notes that mention it + kb article (if any) | Includes note_count, aliases, canonical_name | Low |

---

## Lists Router (`lists.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/lists` | POST | Create empty list (kind='list') | title required (non-empty); generates list/ path; commits | High |

---

## Locations Router (`locations.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/locations` | POST | Ingest location fix (single or batch) | LOOSE auth: full key OR per-person location key; dedup within 100m + 60min for THIS source; updates geofence state; triggers trip detection | High |
| `/api/locations/{person_id}/trail` | GET | Location trail for one person (paginated, optional time window) | Newest first; include accuracy_m, speed_mps, bearing, altitude; owner-only | Med |

**Locations Details:**
- per-person location key scopes fix source to a family member
- Dedup rule: MIN_METERS=30m OR MIN_MINUTES=60min (prevents jitter bloat, allows stationary tracking)
- Geofence refresh is best-effort (never breaks ingest)
- Trip detection rewind is best-effort
- No explicit "current location" endpoint (trail latest is implict)

---

## Places Router (`places.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/places` | GET | List all geofences (places) | Returns id, name, lat, lon, radius_m, note_slug; order by name | Low |
| `/api/places` | POST | Add geofence | name required (unique case-insensitive); radius_m clamped to [20, 20000]; creates loc/<name> note | High |
| `/api/places/{place_id}` | PATCH | Edit place (rename + resize) | Either field optional; blocks if name already exists; updates linked loc/ note title | High |
| `/api/places/{place_id}` | DELETE | Delete geofence + linked note | Soft-deletes both; commits | High |

---

## People Router (`people.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/people` | GET | List all people | Ordered by is_default DESC, then name; ensures default exists | Low |
| `/api/people/owner` | GET | Get owner (default person) state | Returns id, name (stored), display (what prompts use), aliases, is_set | Low |
| `/api/people/owner` | PUT | Set owner name + aliases | Renames default person; refuses if location key present; reconciles kb/People page + alias decisions; commits | High |
| `/api/people` | POST | Create person | name required; location_key optional (scopes family location/notes); commits | High |
| `/api/people/{person_id}` | PUT | Update person (rename, aliases, location_key) | Refuses location_key revoke if owner; reattributes trips; commits | High |
| `/api/people/{person_id}/revoke-location-key` | POST | Revoke per-person location key | Only if not owner; clears location_key; reattributes; commits | High |
| `/api/people/{person_id}` | DELETE | Delete person (except default) | Soft-delete or hard; reattributes fixes to unnamed; commits | High |

---

## Events Router (`events.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/events/{event}` | POST | Fire allow-listed client event (debounced) | event ∈ {wiki_viewed}; debounced 600s per event; fires subscribed event-workflows; returns {fired, debounced} | High |

**Events Details:**
- wiki_viewed fires when a note is opened (drives link-label audit's on-view flag)
- Debounce prevents rapid re-fires from rerunning workflows
- Workflows committed in the fire call

---

## Calendar Router (`calendar.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/calendar/upcoming` | GET | Live, future events (one-offs + next occurrence of recurring) | within_days clamped to [1, 3650]; limit 500 rows then slice; soonest first | Low |
| `/api/calendar/history` | GET | Past events (limit 100, newest first) | Filters status != cancelled/done | Low |
| `/api/calendar/add` | POST | Quick-add event (writes dated note + deterministic projection) | source='manual'; skips LLM extract; projects structured event; commits | High |
| `/api/calendar/remove/{event_id}` | POST | Dismiss event (revoke calendar entry WITHOUT deleting note) | Hides row, stops re-derivation; reversible (/undismiss); commits | High |
| `/api/calendar/{event_id}/undismiss` | POST | Re-enable dismissed event | Cancels dismissal; commits | High |

---

## Tiles Router (`tiles.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/tiles/{z}/{x}/{y}.png` | GET | Map tile proxy + cache (OpenStreetMap) | PUBLIC, unauthenticated (imagery only); caches aggressively (7d); validates z/x/y coords; 502 on fetch fail | Low |

**Tiles Details:**
- Intentionally unauthenticated (no bearer token in URL, preserves CSP 'self')
- Proxies + caches OSM tiles server-side (respects OSM, prevents direct API calls)
- Sensitive data (location trail) stays behind access key on /api/locations
- Best-effort cache write (no-op on failure)

---

## Share Router (`share.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/share/{token}` | GET | Read shared note (public, UNAUTHENTICATED) | Rate-limited; resolves token to exactly one note; bind link → requires claim first or locked to other browser | Med |
| `/api/share/{token}/claim` | POST | Accept bind link; lock to THIS browser (cookie) + return note | Cross-site rejection (sec-fetch-site); idempotent for already-bound browser; commits | High |
| `/api/share/{token}/propose` | POST | Submit edit proposal (public EDIT links only) | proposer_name, content_md, optional note; posts Review card; rate-limited per IP; commits | High |
| `/api/share/{token}/guided/start` | POST | Start guided intake session (public, isolated AI) | Cross-site rejection; resumes if existing session active on THIS browser; non-editable consent injected | High |
| `/api/share/{token}/guided/turn` | POST | Guided session message turn | Cross-site rejection; bills LLM call; session cookie required | High |
| `/api/share/{token}/guided/submit` | POST | Submit guided session (transitions to review pending) | Ends session; posts Review card; session cookie required | High |
| `/api/share/{token}/research/start` | POST | Start research Q&A session (public, scope-bounded AI) | Cross-site rejection; resumes if existing active; samesite=strict cookie | High |
| `/api/share/{token}/research/turn` | POST | Research session message turn | Cross-site rejection; bills LLM call; samesite=strict cookie | High |
| `/api/share/{token}/research/labs/series` | GET | One analyte's scoped series (research links only) | Identity-stripped; clamped to owner window; samesite=strict cookie required; validates against scope | High |
| `/api/share/{token}/research/attachments/{att_id}` | GET | Fetch one scoped attachment (research links) | Validates against approved list; no bearer key in URL (public, same-origin only) | Med |

**Share (Public) Details:**
- Rate limit: keyed on client IP (X-Forwarded-For honored), per-token buckets
- Bind links: lock to first browser (cookie); cross-site form POST rejection; 409 on race
- Edit proposals: staged, not applied until owner approves via share_admin
- Guided: AI has NO brain access, consent + intro injected, responses await approval before ingest
- Research: AI reads ONLY owner-approved note allowlist, no search, per-link lab analyte scope
- Labs: identity-stripped (date only, no personal details), clamped to configured owner window
- Public attachments: direct <img>/<audio>/<video> via public_router (no token in URL needed, loaded same-origin)

---

## Share Admin Router (`share_admin.py`)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/shares` | POST | Mint share link (owner-gated) | scope ∈ {view, edit}; optional bind + ttl_days; returns token + share_url; commits | High |
| `/api/shares` | GET | List all share links + proposals + history | Active links, pending proposals (with diff preview), acceptance/rejection history, guided + research links + sessions | High |
| `/api/shares/{link_id}` | GET | Fetch one share link detail | Full config, usage stats, bound name | Low |
| `/api/shares/{link_id}` | PUT | Update share link (label, TTL) | Partial update; commits | High |
| `/api/shares/{link_id}` | DELETE | Revoke share link | Deactivates; commits | High |
| `/api/shares/proposals/{proposal_id}/approve` | POST | Apply edit proposal to wiki + mark accepted | Applies via staging._apply_action; records acceptance; commits | High |
| `/api/shares/proposals/{proposal_id}/reject` | POST | Discard proposal + mark rejected | Records rejection; commits | Med |
| `/api/shares/guided/{session_id}/approve` | POST | Ingest guided session (AI interview) + post Review card | Approves + writes to note; deletes transcript (owner review only); commits | High |
| `/api/shares/guided/{session_id}/reject` | POST | Discard guided session + mark discarded | Transcript deleted; commits | Med |
| `/api/shares/research/{session_id}` | GET | Fetch research session (Q&A transcript) | Returns full session detail for owner review | Low |
| `/api/shares/research/{session_id}/approve` | POST | Accept research session (no ingest, just approval) | Marks completed; commits | High |
| `/api/shares/research/{session_id}/reject` | POST | Reject research session | Marks discarded; commits | Med |

---

## Health Endpoint (main.py)

| Route | Method | Intent | Key Behaviors | Risk |
|-------|--------|--------|---------------|------|
| `/api/health` | GET | Liveness probe (public) | Returns {ok: true, brain: settings.brain_name}; not used for auth | Low |

---

## Summary & High-Risk Clusters

### Total Endpoints
**115+ HTTP endpoints** across 20+ routers, including:
- **Auth & Status:** 2 auth endpoints (info, verify) + soft-auth status endpoint
- **Core Wiki:** 20 note endpoints (CRUD, analysis, talk, history, restore, versions)
- **Chat & Search:** 6 chat endpoints (conversations, messages, streaming) + 1 hybrid search
- **Attachments:** 10 endpoints (upload, analyze, transcribe, stream, download, delete)
- **Workflows & Actions:** 11 workflow + 6 action-def endpoints (full CRUD + run/sync)
- **Medical/Financial:** 12 medical + 2 financial endpoints (capture, lab parsing, analytes/trends)
- **Sharing & Public:** 12 public share endpoints + 10 admin share endpoints (proposals, guided, research, labs)
- **Entities & KB:** 8 entity endpoints (merge, split, alias) + 8 rebuild endpoints (interactive AI rewrite)
- **Geo & Entities:** 5 location endpoints + 4 place endpoints + 7 calendar + 4 people endpoints
- **Data & System:** SQL console, prompts, reviews, staging (verify + apply), events, tiles

### High-Risk Clusters

#### 1. **Data Mutation & State Changes (Highest Criticality)**
   - **Notes CRUD:** create/update/delete/restore (file:line notes.py:313-482)
     - Optimistic concurrency guards (basis hash) on UPDATE
     - Soft-delete with recovery via restore (versions)
     - No hard delete; SQL DELETE only on versions
   - **Staging Apply:** transactional architect changes (file:line staging.py:157-300)
     - Basis staleness guard (409 if note changed since proposal)
     - Dead-link verification before write
     - Fabrication checks (link target must exist)
   - **Workflows & Scheduled Actions:** run on background scheduler (file:line workflows.py:107-113)
     - Trigger evaluation off main event loop (60s intervals)
     - Location triggers evaluated during scheduler pass (not at ingest)
     - Can fire external actions (HTTP, email, API calls)
   - **Lab Ingest:** approve moves unapproved → canonical (file:line medical.py:127-130)
     - One approval moment; no undo except revoke + reanalyze
     - Wrong-patient flag (DOB check) before approve
   - **Entity Decisions:** merge, split, alias are durable + trigger rebuild (file:line entities.py:40-117)
     - Decisions recorded synchronously; rebuild deferred
     - UI must poll /status to see fold materialize (eventual consistency)

#### 2. **External Calls & Data Egress (PII/PHI Risk)**
   - **External Lookups:** approve sends term to health APIs (file:line external.py:22-36)
     - Owner can EDIT term before send (trim PHI)
     - Only approval moment sends data (no PII leakage at propose)
     - Caches result so no re-fetch
   - **System Update Check:** fetches GitHub API (cached 1h) (file:line system.py:113-134)
     - GitHub API could be rate-limited or unavailable (graceful degrade)
   - **Tile Proxy:** public calls to OpenStreetMap (file:line tiles.py:31-58)
     - Location trail itself stays behind access key
     - Tiles are public imagery (no PII in proxy headers)
   - **LLM Calls:** architect, medical ref, image analysis, transcription (file:line chat.py:114-187, rebuild.py, etc.)
     - Model choice + credentials checked at call time
     - Streaming SSE with 15s keepalive (no timeout drops)
     - Chat mode enforces access control (assisted vs research)

#### 3. **Authentication & Access Control (Auth/Authz Risk)**
   - **Bearer Token Verify:** CurrentUser dependency on all protected routes (file:line auth.py)
     - Soft-auth on /api/system/status (returns skeleton on invalid key)
     - Hard-auth (401) on all /api/auth-protected routes
   - **Per-Person Location Key Scoping:** family members can only send fixes (file:line locations.py:56-100)
     - Dictation capture accepts both full + per-person keys (file:line notes.py:406-459)
     - Fixes attributed to person (for source tracking in notes)
   - **Share Links:** token-based access (no bearer key) (file:line share.py)
     - Rate limit keyed on client IP (prevents brute-force token enumeration)
     - Bind links lock to first browser (sec-fetch-site check, CSRF protection)
     - Cross-site form rejection on claim/turn/submit (drive-by acceptance blocked)
     - Edit proposals staged (not auto-applied), guided/research sessions await approval
   - **Public Routes:** /api/tiles, /api/share/{token}*, /api/attachments/{id}/stream (token-gated)
     - Tiles: PUBLIC (no auth); imagery only, no user data
     - Share: public token-based access; rate-limited + bind-locked
     - Media streaming: signed token per-attachment (not bearer in URL)

#### 4. **LLM-Powered Features (Reasoning & Jailbreak Risk)**
   - **Chat/Architect:** full-brain assistant (write tools) vs research-only (read tools) modes (file:line chat.py:22-59)
     - Mode fallback to read-only on unknown mode (fail-safe)
     - Fresh context clears prior grounding (resists injection across turns)
     - Location optional (lat/lon/label captured for context)
   - **KB Rebuild:** multi-stage interactive rewrite (gather → draft → guide → accept) (file:line rebuild.py)
     - Stage 1 (gather): AI proposes sources; human curates before write
     - Stage 2 (draft): LLM writes from curated sources
     - Guide: owner steers mid-flight revision
     - Accept: staleness guard (base hash) prevents clobbering intervening edits
   - **Medical Reference:** RxNorm + MedlinePlus external lookups (file:line external.py:30-36)
     - Owner approval required before any send (owner edits term first)
     - Result cached (no re-fetch)
   - **Image Analysis & Transcription:** async enrichment (file:line attachments.py:29-112)
     - Gated by "auto-analyze new notes" toggle
     - Image vision requires LLM credentials (best-effort; no-op if unavailable)
     - Audio/video transcription via local faster-whisper (no external call)
   - **Guided Intake:** isolated AI with NO brain access (file:line share.py:159-258)
     - Recipient interviewed by guide_svc; no search, no notes access
     - Consent + disclaimer injected by server (non-editable by owner)
     - Session awaits owner approval before ingest
   - **Research Q&A:** scope-bounded AI (approved note allowlist only) (file:line share.py:260-339)
     - AI reads ONLY per-link approved notes
     - Per-link lab analyte scope (charts identity-stripped, clamped)
     - Lab series validated against scope (not just client-side)

#### 5. **Concurrency & Race Conditions**
   - **Staging Apply Staleness:** optimistic concurrency via content_hash (file:line staging.py:173-187)
     - Refuse UPDATE if basis hash != current (409 "stale")
     - Matches nightly KB save pattern
   - **KB Rebuild Accept:** base hash guard + KB lock (file:line rebuild.py:209-230)
     - Lock acquired during accept (blocks maintenance)
     - Compare current hash to run.base_hash; refuse if changed (409)
   - **Entity Merge Deferred Rebuild:** decision record durable before rebuild queued (file:line entities.py:40-66)
     - Synchronous decision insert; deferred rebuild coalesced
     - UI polls /status to track rebuild progress (eventual consistency)
   - **Lab Approval Dedup:** one lab_status per attachment (file:line medical.py:127-137)
     - Approve marks lab_status='ingested' (idempotent)
     - Revoke clears it (re-approves allowed)
   - **Session Cookies & Bind Links:** samesite=lax (guided) | samesite=strict (research) + httponly (file:line share.py:184-188, 284-289)
     - Prevents CSRF (sec-fetch-site check on POST)
     - httponly prevents XSS script access

#### 6. **Data Validation & Injection Prevention**
   - **Wikilink Dead-Link Check:** neutralize [[Target]] that resolves to no live note BEFORE write (file:line staging.py:172)
     - Prevents fabricated links from reaching saved articles
   - **YAML/JSON Parsing:** safe_load (YAML) for recipes + validation (file:line action_defs.py:22-29)
     - Validates recipe structure (warnings on invalid)
     - Refuses non-mapping YAML
   - **SQL Console:** sqlsafe.run_select restricts to SELECT/WITH only (file:line sql_console.py:21-29)
     - No INSERT/UPDATE/DELETE
     - Errors surfaced to UI (SQLite error strings)
   - **Staging Content Verify:** verify_content checks links before write (file:line staging.py:172)
     - Calls staged_verify.verify_content (hard backstop)
   - **Attachment MIME Filtering:** neutralize svg+xml, text/html, javascript → application/octet-stream (file:line attachments.py:219-220)
     - Forces download (no XSS inline render)
     - X-Content-Type-Options: nosniff
   - **Note Title Sanitization:** calendar quick-add sanitizes newlines + brackets (file:line calendar.py:34-37)
     - Blocks [[...]] injection in quick-add title field

#### 7. **Database Concurrency & Locking**
   - **KB Lock:** explicit lock() / unlock() around maintenance pass (file:line rebuild.py:212-230)
     - Acquire during accept; refuse if maintenance running (409)
   - **Transaction Scope:** most writes auto-commit via conn.commit() (ACID guarddependencies)
     - Rollback on exception (notes.py, staging.py, etc.)
   - **Soft Deletes:** deleted_at timestamp (recoverable via restore)
     - Hard DELETE on some internal tables (message_steps, etc.)
   - **Write-Lock Recovery:** 503 + descriptive message on locked DB (file:line notes.py:479-481)
     - "database was busy"; suggests retry

### Notable Security Controls

1. **Rate Limiting:** Share link resolution rate-limited per-token + per-IP
2. **CSRF Protection:** sec-fetch-site check on state-changing share endpoints (claim, turn, submit)
3. **Bind Links:** Browser cookie locking (first acceptor only); samesite=lax/strict
4. **Media CSP:** Signed token per-attachment (no bearer in URL); <img>/<audio>/<video> can load without CSP media-src
5. **Export/Backup:** Non-destructive updates; DB + .env preserved on self-update
6. **Consent Injection:** Guided/research landing pages inject server-side disclaimer (non-editable)
7. **Identity Stripping:** Research lab charts identity-stripped (date only); clamped to owner window
8. **Staged Application:** Edit proposals staged (not auto-applied); guided sessions await owner approval before ingest
9. **Optmistic Concurrency:** Basis hash guard on UPDATE (refuse stale edits)
10. **Soft Deletes + Restore:** Notes + places soft-delete (recoverable); no cascade deletes

---

## Surprising Findings

1. **No Explicit Pagination on Most Reads:** list endpoints default to 200-500 rows; no cursor/offset pagination (e.g., GET /api/notes lists all non-deleted notes up to limit)
2. **Entity Rebuild Deferred & Coalesced:** Merge/split/alias decisions are synchronous (durable immediately), but fold materialization is async + polled by UI (eventual consistency)
3. **Staging Area as Approval Gate:** ALL architect edits (CREATE/UPDATE/DELETE/etc.) are staged + require manual apply (no auto-commit)
4. **Bind Links for Shared Notes:** Owner can lock share links to the first browser that opens them (sec-fetch-site + cookie locking)
5. **Lab Extraction Pre-Approval:** Medical PDFs are deterministically parsed (no LLM) and staged for review; nothing reaches canonical lab_results until owner approve
6. **Per-Person Location Keys:** Family members can send location fixes + dictations with a location-scoped key (not full access key)
7. **Guided & Research Isolation:** Isolated AI instances for public intake (NO brain access); responses await owner approval before ingest
8. **KB Lock During Rebuild Accept:** Acquire lock to block maintenance while accepting a rebuild (prevents race with nightly synthesis)
9. **Talk (Article Maintenance Memory):** Separate "talk" entries (notes, questions, directives) beside articles; owner ↔ AI conversation that feeds next maintenance pass
10. **SQL Console Read-Only:** Full SELECT access (including FTS, joins, aggregates); no INSERT/UPDATE/DELETE; error strings surfaced to UI

---

## Conclusion

The JBrain API surface is **large but well-scoped**: 115+ endpoints organized by domain (notes, chat, attachments, workflows, medical, sharing, etc.). Key features include:

- **Staged application:** All AI-proposed edits are staged for human review (no silent auto-writes)
- **Eventual consistency:** Entity rebuilds + workflow runs are async, UI polls for progress
- **Multi-layered sharing:** Public + authenticated routes; bind links; scoped access (research, guided, labs)
- **Per-person scoping:** Location keys + people registry enable family tracking without full access key
- **Concurrency guards:** Optimistic concurrency (hash-based staleness) + KB lock + entity rebuild coalescing
- **LLM orchestration:** Chat modes (assisted/research), KB rebuild stages, guided intake, research Q&A
- **Data validation:** Dead-link checks, YAML parsing, MIME filtering, SQL restriction (SELECT-only console)

**Risk profile:** Highest risk clusters are data mutations (notes, staging, workflows, labs), external calls (health APIs, GitHub), and LLM-powered features (assistant, rebuild, medical ref). All are protected by staged application, auth gating, and concurrency guards. No hard data deletion (soft deletes + restore); all writes are traceable (versions, source attribution).

