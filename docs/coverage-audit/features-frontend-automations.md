# JBrain Features: Frontend, Automations & Android

## 1. FRONTEND PWA (web/src/)

### Auth & Core Setup
- **Auth context & verification**: Bearer token auth, server config (App.tsx, api.ts)
- **Key entry & onboarding**: Manual key paste (KeyEntry), optional owner setup (OwnerOnboarding)
- **Version mismatch detection**: PWA vs server version tracking, update prompts (SystemPage)
- **Risk**: **HIGH** — auth fallback logic (401 vs 5xx) must be carefully tested; offline scenarios unclear

---

### Primary Compose Modes (Chat.tsx - 56KB)
Three top-level UX modes; Entry splits into 3 sub-types (generic/medical/financial):

#### Entry (write-only, no AI)
- **Intent**: Quick capture to wiki; no inference latency
- **Sub-types**: Generic (dated tree) | Medical (notes/medical/) | Financial (notes/financial/)
- **Key files**: Chat.tsx (lines 29–75), api.ts (createEntry)
- **Observable behaviors**:
  - Mode/sub-type stored in sessionStorage, with legacy remapping (medical→entry)
  - Medical/Financial show placeholder "Log a lab/statement" but safety copy differs
  - No API calls until final "Save entry" (debounced)
  - Attachments must pass MAX_ATTACHMENT_BYTES check (~10MB typical)
- **Risk**: **MED** — mode normalization fallback (DEFAULT_MODE='research') may mask UI bugs if sessionStorage gets stale

#### Research (read-only, assistant)
- **Intent**: Query the brain without touching notes; safe exploration
- **Key files**: Chat.tsx (mode='research'), api.ts (streamChat)
- **Observable behaviors**:
  - Full tool access: reference lookup, medical/financial searches, entity graph reads
  - No write tools; any proposed edits are staged (visible in /shares, not applied)
  - LLM streaming via SSE; conversation persists in Chat.tsx state
  - TTS available for replies (useTts hook)
- **Risk**: **LOW** — read-only isolation is well-enforced

#### Full Brain (write + propose + edit)
- **Intent**: Assistant proposes and auto-applies changes with undo
- **Key files**: Chat.tsx (mode='full'), api.ts (streamChat, approveExternalLookup, denyExternalLookup)
- **Observable behaviors**:
  - Tool access includes write, KB search, external lookup gates (user must approve/deny)
  - LLM can propose wiki edits, tag notes, file captures, create review items
  - stagingPanel shows pending change log before user commits
  - "Undo" via note history (versioning server-side)
- **Risk**: **HIGH** — external lookup approval UX is critical; a missed rejection could mislead the LLM into dangerous actions

---

### Wiki & Knowledge Base (Wiki.tsx, NotePage.tsx)

#### Wiki Browser (Wiki.tsx)
- **Intent**: Hierarchical note directory with live search and filters
- **Key files**: Web/src/pages/Wiki.tsx
- **Observable behaviors**:
  - "/" title separators → tree display (kb/Places/Africa → nested)
  - Filters: All | Entries | Lists | Places | KB (cached in URL params)
  - Auto-collapse except the branch containing the most recently updated note
  - fireEvent("wiki_viewed") triggers event-workflows
  - Link audit panel (LinkAuditPanel) checks for stale [[Target|Display]] labels
- **Risk**: **MED** — tree collapse logic on first load could race with async API call; the "keepOpen" set might be stale

#### Note Page (NotePage.tsx - 24KB)
- **Intent**: Full-fidelity note view, edit, share, version history
- **Key files**: web/src/pages/NotePage.tsx, components/Attachments.tsx, VersionViewer.tsx
- **Observable behaviors**:
  - Render Markdown with wiki links, citations, addresses (linkified)
  - Full edit with live markdown preview (ListEditor for list notes)
  - Attachment manager (upload, delete, replace)
  - Version timeline: click to view/diff (MarkdownDiff component)
  - AI rebuild panel (RebuildPanel) — LLM gather+draft for KB articles only
  - Share options: TTL, device binding, editable-by-recipient toggle
  - Tags, backlinks, location metadata, KB/tool-access toggles
  - Redirect handling (merged-away notes forward to canonical)
- **Risk**: **HIGH** — edits save immediately on blur; no explicit "Save" button. Race conditions possible if two tabs edit simultaneously. Version history diff rendering (canvas-based) must be stress-tested on large documents.

---

### Search & Discovery (SearchPage.tsx)

#### Hybrid/Keyword/Semantic/Entities Search
- **Intent**: Fast, multi-modal note lookup with relevance scores
- **Key files**: web/src/pages/SearchPage.tsx, api.ts (GET /api/notes/search)
- **Observable behaviors**:
  - Mode selection: hybrid (keyword + vector blend) → keyword (BM25) → semantic (embeddings only) → entities (entity index)
  - Semantic mode auto-falls-back to hybrid if embeddings model is warming/unavailable (SearchPageGating.test.tsx validates)
  - Results show relevance badge: semantic = cosine similarity %; hybrid = score normalized to best hit
  - Keyword mode has no badge (no absolute scale)
  - Entity hits show type glyph (person/place/org/etc), article link if available
  - Search params mirror to URL (replace mode, so Back returns to populated search)
- **Risk**: **MED** — semantic fallback logic is crucial for UX (no dead hangs); warmup detection must be reliable

---

### Chat Shares & Collaboration (SharesPage.tsx - 27KB)

#### Standard Share Links
- **Intent**: Temporal + device-bound read/edit links to notes
- **Key files**: web/src/pages/SharesPage.tsx, api.ts (shares endpoints)
- **Observable behaviors**:
  - Scope: view | edit
  - TTL (days, 0=never), device binding (bind=1), editable flag
  - Revoke immediately; reset-bind to unlock device lock
  - Recipient URL includes token (stateless verification)
  - If editable: proposals staged, owner approves/rejects (versioned undo)
- **Risk**: **MED** — device binding state (bind + bound_at) is server-enforced; UI must clearly show if a link is device-locked

#### Guided Share Links (ChatShareLinks.tsx)
- **Intent**: Goal-driven structured interviews; conversational data capture
- **Key files**: SharesPage.tsx, ChatShareLinks.tsx, GuidedChat.tsx
- **Observable behaviors**:
  - Recipient opens a link, follows a conversational flow (AI-guided Q&A)
  - Goal + intro + sub-prompt define the interview
  - Single-use or multi-start flag; bind/expiry same as notes
  - Sessions: pending (in progress) | ended (transcript captured) | history (archived)
  - Document upload capture + transcript storage server-side
- **Risk**: **HIGH** — transcript storage + PII in conversation history; deletion must be tested. The sub_prompt field could leak system instructions if misconfigured.

#### Encrypted Chat Links (ChatShareGuest.tsx)
- **Intent**: End-to-end encrypted peer chat (guest + owner)
- **Key files**: EncryptedChat.tsx
- **Observable behaviors**:
  - Web Crypto API AES-GCM encryption (browser-side, server stores ciphertext only)
  - Guest identity optional (name field)
  - Message history persisted (encrypted) on server
  - Owner can view conversations from /shares/chat/:linkId
- **Risk**: **HIGH** — crypto implementation must be audited. Key derivation (PBKDF2 from token?) must be bulletproof. Server-side CORS/origin checks are essential.

#### Research Links & Lab Shares
- **Intent**: Expose specific KB articles or lab charts to external readers
- **Key files**: ResearchLinks.tsx, LabShareLinks.tsx
- **Observable behaviors**:
  - ResearchLinks: read-only article + citation links
  - LabShareLinks: embed lab chart (LabChart.tsx) for a specific analyte + time range
  - Both support expiry; no editing/proposals
- **Risk**: **LOW** — read-only; no mutations

---

### Inbox & Review (ReviewPage.tsx)

#### Review Items
- **Intent**: Actionable cards from workflows (daily rollups, KB audit flags, manual prompts)
- **Key files**: web/src/pages/ReviewPage.tsx, api.ts (reviews endpoints)
- **Observable behaviors**:
  - Each item has title, optional message, link_slug (deep-link to entry)
  - Dismiss → removes from inbox (POST /api/reviews/:id/dismiss)
  - Timestamps in app timezone (appTz from auth verify)
  - No prioritization or snooze (stateless list)
- **Risk**: **LOW** — simple list UI; main risk is workflow delivery accuracy

---

### Knowledge Base Tools

#### Workflow Execution & Live Monitoring (WorkflowsPage.tsx - 27KB)
- **Intent**: Trigger automation runs, view status, edit config
- **Key files**: web/src/pages/WorkflowsPage.tsx, api.ts
- **Observable behaviors**:
  - List all workflows (id, name, trigger_type, action_type, enabled, locked, last_status, last_run_at)
  - Toggle enabled flag (locked workflows are read-only after repo ingest)
  - "Run now" button triggers manual execution
  - Live pipeline watch modal (PipelineView.tsx) streams step names + statuses via polling
  - Step labels (STEP_LABELS dict) humanize pipeline primitive names
  - Config editor (ConfigFields.tsx) for per-workflow tuning (batch limits, prompts, flags)
- **Risk**: **MED** — manual runs should be debounced (prevent double-click). Live watch via polling could lag or miss fast steps.

#### SQL Console & Backup (SqlConsole.tsx - 5KB)
- **Intent**: Power-user data inspection and backup/restore
- **Key files**: web/src/pages/SqlConsole.tsx, api.ts
- **Observable behaviors**:
  - Free-form SQL query executor; example queries provided
  - Results: columns + rows grid
  - Export database (SQLite backup download)
  - Import database (file picker → full restore with confirmation modal)
  - "Export original notes" button (pre-AI versions of all live notes)
- **Risk**: **HIGH** — restore is DESTRUCTIVE (replaces DB). UI must have explicit confirmation. No undo after restart. SQL injection surface (server validates).

---

### Temporal & Location Features

#### Calendar (CalendarPage.tsx - 29KB)
- **Intent**: Events + reminders view with custom event types and recurrence
- **Key files**: web/src/pages/CalendarPage.tsx, api.ts (cal* endpoints)
- **Observable behaviors**:
  - Views: list | day | week | month
  - Event kinds: event | appointment | deadline | reminder
  - Reminders: timed (0/10/30min, 1/2/1d) or all-day (morning of, evening before, 2d before)
  - Reminder persistence via Reminder struct (offset_minutes, anchor)
  - Quick-add modal for new events
  - Mark reviewed / dismiss (calMarkReviewed, calDismiss)
  - Events are owner-local wall-clock (no TZ conversion; event strings treated verbatim)
- **Risk**: **MED** — TZ handling is implicit (Intl.DateTimeFormat); server TZ conflicts could cause off-by-one-day errors. Reminder offsets for all-day events use a 9am anchor (non-obvious).

#### Map & Location Tracking (MapPage.tsx - 32KB)
- **Intent**: Multi-person location trail visualization + place tracking
- **Key files**: web/src/pages/MapPage.tsx, api.ts (location/place endpoints)
- **Observable behaviors**:
  - Trail view: person-colored polyline, simplified via Douglas–Peucker (preserves endpoints)
  - Heatmap view: dwell density via leaflet.heat
  - Date-range presets (1h / 3h / 12h / 24h / 1w / all)
  - Scrub timeline with play button
  - Tap-on-map → "what notes are here" (200m radius)
  - Saved places: create, update, delete via map click
  - Tiling via server proxy (browser never talks to third-party tile host)
  - Per-person person-note authored in server response
  - Zoom-adaptive line simplification (epsilonForZoom)
  - Vertex cap (3000 max) prevents performance explosions
- **Risk**: **HIGH** — GPS trail math is complex (haversine, DP simplification, stride decimation). Large trails could cause frame drops. Location privacy (stored server-side; shared on public links) must be clearly warned.

#### People & Location Keys (PeoplePage.tsx - 7KB)
- **Intent**: Manage device identities and location tracking personas
- **Key files**: web/src/pages/PeoplePage.tsx, api.ts
- **Observable behaviors**:
  - People aren't accounts; they label location trails + watch dictations
  - Exactly one is default ("Me") — catch-all for unmatched sources
  - Setup code (jbt1.<base64url(JSON)>) encodes name+server+token in one string for phone/watch paste
  - Per-person: name, is_default, location_key (generated), aliases
  - Revoke key (device stops uploading)
  - Color trails on map per person
- **Risk**: **LOW** — setup code format is stable; base64url encoding must survive QR-code-scan round-trips

---

### Medical & Lab Features

#### Medical Page (MedicalPage.tsx - 4KB)
- **Intent**: Jumping-off point for medical notes + lab tracking
- **Key files**: web/src/pages/MedicalPage.tsx
- **Observable behaviors**:
  - Links to notes/medical/ folder + Labs page
  - Minimal page; mostly navigation
- **Risk**: **LOW** — thin wrapper

#### Labs Page (LabsPage.tsx - 11KB)
- **Intent**: Chart + table view of lab results (analytes) over time
- **Key files**: web/src/pages/LabsPage.tsx, components/LabChart.tsx, LabShareCreator.tsx
- **Observable behaviors**:
  - Analyte picker (searchable, abnormal-first sort)
  - Time-window presets (1y / 5y / all), persistent across analyte switches via localStorage (best-effort)
  - Chart visualization (LabChart.tsx): line+scatter, with reference ranges shaded
  - Table toggle (show raw values + flags)
  - Status pip (▲/▼/●/○ + color) shows high/low/normal/unknown
  - Pending labs queue (LabImportPanel component not shown here, but imported in NotePage)
  - Share lab chart via LabShareLinks (guest can embed + view)
- **Risk**: **MED** — localStorage is best-effort (private browsing fails silently). Window selection logic (keepWin) could be fragile if a series has no points in the saved range.

---

### Entities & Knowledge Graph

#### Entities Browser (EntitiesPage.tsx - 13KB)
- **Intent**: Navigate semantic entities (people, places, orgs, conditions, etc.)
- **Key files**: web/src/pages/EntitiesPage.tsx, api.ts
- **Observable behaviors**:
  - Entity types: person | animal | org | place | thing | work | condition | medication | procedure | event | concept
  - Per-entity: canonical name, aliases, note count, article link (if exists in KB)
  - Search + type filter
  - Entity glyphs (👤 / 🐾 / 🏢 / 📍 / 📦 / 🎬 / 🩺 / 💊 / 🩻 / 📅 / 💡)
  - Link to note/article (if any)
- **Risk**: **LOW** — read-only browser

#### Knowledge Graph (GraphPage.tsx - 13KB)
- **Intent**: Force-directed graph of note connections (entry/KB/list)
- **Key files**: web/src/pages/GraphPage.tsx, react-force-graph-2d library
- **Observable behaviors**:
  - Render nodes (entry=sky, kb=amber, list=purple) + edges (backlinks)
  - Kind filter (all | kb | entry | list)
  - Focus + depth control (1 hop default; KB uses 2 hops)
  - Node tap → navigate to note
  - Collision + repulsion forces; responsive canvas sizing
  - Douglas–Peucker label placement (avoid overlap)
- **Risk**: **MED** — large graphs (thousands of nodes) could lag. Canvas rendering must be benchmarked on older devices.

---

### System & Settings (SystemPage.tsx - 18KB)

#### Update Management
- **Intent**: Live server update trigger + progress monitoring
- **Key files**: web/src/pages/SystemPage.tsx, components/UpdateConsole.tsx
- **Observable behaviors**:
  - Check version endpoint (GET /api/system/version)
  - Trigger update (POST /api/system/update)
  - Three deploy modes: started (auto-restart) | scheduled+auto (auto-restart) | scheduled (manual host step)
  - Health poll (15s timeout, 90s fail threshold) watches for server restart
  - If deploy fails: show diagnostic commands (cd ~/JBrain; docker compose logs…)
  - UpdateConsole modal shows live docker logs (if API comes online)
- **Risk**: **HIGH** — restart detection is polling-based (15s + 90s = ~2 min overhead). If the server is down for >90s, update is marked failed. Docker logs stream must be handled carefully (large logs could OOM).

#### Settings & Preferences
- **Intent**: LLM model picker, voice selection, location toggle, notifications
- **Key files**: SystemPage.tsx, components/ModelPicker.tsx, MediaSettings.tsx, AutoAnalyzeSetting.tsx
- **Observable behaviors**:
  - Owner name + timezone (OwnerSetting.tsx)
  - LLM model picker: select from available models (via capabilities)
  - Voice picker: TTS voice selection; English-filtered by default, all-languages toggle
  - Voice preview: play sample via Web Speech API (client-side, no streaming)
  - Location tracking toggle (sets geo opt-in flag)
  - Push notifications: test + delay slider
  - Auto-analyze toggle (system flag; feeds analyze-new-note workflow)
- **Risk**: **MED** — TTS voice selection is browser-dependent (voice list varies). Web Speech API is non-standard (some browsers lack it). Location toggle is UI; server-side enforcement must be checked.

#### Prompts Editor (PromptsPanel.tsx)
- **Intent**: Live edit of system prompts (modes, tool descriptions, workflow actions)
- **Key files**: web/src/components/PromptsPanel.tsx, api.ts (prompts endpoints)
- **Observable behaviors**:
  - Read prompts.yaml schema from server (modal form per prompt category)
  - Edit + save (POST /api/prompts)
  - Changes picked up immediately by agent (no restart)
  - Large form (110KB config) → incremental load via category tabs
- **Risk**: **HIGH** — prompt injection risk if user-supplied text leaks into system prompts. Whitelist validation on server is essential.

---

### Attachments (Attachments.tsx)

#### File Upload & Management
- **Intent**: Associate files (PDFs, images, etc.) with notes
- **Key files**: web/src/components/Attachments.tsx, api.ts (attachment endpoints)
- **Observable behaviors**:
  - Drag-drop or file picker
  - File size check (MAX_ATTACHMENT_BYTES, typically ~10MB)
  - Upload progress bar
  - Metadata: file name, mime type, size
  - Delete + replace
  - For medical notes: PDFs auto-staged to staging panel for LLM analysis
- **Risk**: **HIGH** — file uploads are server-side validated (MIME type, size); client-side checks are UX only. Large files could timeout. Virus/malware scanning not mentioned (risky).

---

### UI Components & Utilities

#### Shared Components
- **Shell.tsx**: Top nav (logo, search, health status dot, menu), sidebar routes
- **Modal.tsx**: Generic overlay dialog
- **Toaster.tsx**: Toast notifications (showToast hook)
- **Icon.tsx**: SVG icon map
- **CitationLink.tsx**: Wiki link renderer with hover preview
- **ToolHistory.tsx**: Expandable log of tool calls from a single assistant reply
- **PipelineView.tsx**: Live workflow step status monitor
- **ApprovalView.tsx**: Decision tree UI for external-lookup approval
- **StagingPanel.tsx**: Pending-changes preview before commit
- **ResearchChat.tsx**: Research-mode assistant reply with citations
- **GuidedChat.tsx**: Guided interview conversation view
- **TalkPanel.tsx**: Text-to-speech playback panel
- **RebuildPanel.tsx**: KB article rebuild (gather + draft) live monitor
- **VersionViewer.tsx**: Note diff + timeline (HistoryTimeline, DiffView, TimelineEntry)
- **NoteActionsMenu.tsx**: Context menu (edit, delete, archive, KB rebuild, etc.)

---

## 2. AUTOMATION WORKFLOWS

All workflows live in `/home/user/JBrain/workflows/` (*.yaml) and `/home/user/JBrain/actions/` (*.yaml). Workflows are scheduled (cron/interval) or event-triggered; actions define the pipeline steps.

### Scheduling Patterns

#### Cron-Scheduled (time-triggered)
- **calendar-alarms**: "0 * * * *" (hourly) — check upcoming calendar events
- **calendar-reminders**: "0 7 * * *" (7am daily, RETIRED) — surface events in lead window
- **daily-consolidate**: "0 0 * * *" (midnight) — roll dated captures into daily summary

#### Interval-Scheduled (run-only or disabled)
- **wiki-build**: 31536000s (1 year; enabled=false) — manual-only KB reorganization
- **promote-recurrences**: interval_seconds=0 (manual-only) — detect recurring patterns in chatter

#### Event-Triggered
- **entry_created**: Fires when user creates an entry note
- **wiki_viewed**: Fires when wiki is opened (fireEvent from Wiki.tsx)
- **log_appended**: Generic log append event (custom trigger)

### Workflow Categories & Intents

#### Daily Operations
- **daily-consolidate** (ACTIVE, cron midnight)
  - Action: `consolidate_daily`
  - Intent: Roll each day's dated capture notes (notes/daily/YYYY/MM/DD/<n>) into a single daily summary
  - Config: Optional consolidation prompt override; review card toggle
  - Produces: Single note per day (kind='daily'); review card
  - Risk: **MED** — idempotency relies on existence check, not watermarks. Restarts backfill every still-missing day (could re-summarize if notes are edited).

#### Knowledge Base Build & Maintenance
- **wiki-build** (DISABLED, manual-only)
  - Action: `wiki_build` (7060 bytes in actions/)
  - Intent: Full KB reorganization (soft-delete old articles, rebuild from notes)
  - Steps: analyze_pending → rebuild_entity_index → wiki_outline → wiki_write_batch → validate_structure → review
  - Config: reset (true=wipe first), analyze_limit (max 400 notes to re-analyze), digest_limit (max 3000 notes to survey)
  - Produces: Rebuilt kb/* articles (versioned, undoable); review cards for structure failures
  - Risk: **HIGH** — DESTRUCTIVE. Soft-delete must be tested; protected kb/_* pages must not be touched. Structure lint false-positives could lose articles.

- **wiki-maintain** (config-dependent, likely scheduled nightly via server)
  - Action: `wiki_maintain`
  - Intent: Incremental KB upkeep (find changed entries, plan+stage wiki updates)
  - Steps: query_entry_changes → wiki_plan → wiki_maintain → validate_citations → stage_kb_proposals
  - Produces: Staged article rewrites; review cards
  - Risk: **MED** — citation validation must not break the knowledge graph. Stale proposals from a previous failed run could interfere.

- **wiki-update** (linked from daily-consolidate, incremental)
  - Action: `wiki_update`
  - Intent: Fold newly-drafted daily summaries into KB articles
  - Produces: Updated articles (staged or auto-applied per config)
  - Risk: **MED** — must not conflict with manual wiki edits

- **promote-recurrences** (ACTIVE, manual-only)
  - Action: `promote_recurrences` (4610 bytes)
  - Intent: Find recurring patterns in chatter (notes logged >N distinct days), cluster by similarity, synthesize kb/Patterns articles
  - Config: batch_limit (500 entries scanned), min_days (3), tau (0.35 cosine threshold), promote_limit (5), auto_apply (false=stage)
  - Produces: Staged kb/Patterns articles; optional calendar recurring events
  - Risk: **MED** — clustering is similarity-based (no ground truth). False positives (spurious patterns) could clutter the KB.

- **recite_kb** (name from actions/, ACTIVE, manual-only)
  - Action: `recite_kb` (reformat old-style citations to footnotes)
  - Intent: Citation cleanup (old [[…]] inline style → new [^sN] footnote style)
  - Config: batch_limit (10 articles), auto_apply (false=stage)
  - Produces: Staged article rewrites
  - Risk: **LOW** — read-only detection (skips any article with malformed footnotes); guarded by validate_citations

#### Analysis & Tagging
- **analyze-new-note** (DISABLED by default, event-triggered on entry_created)
  - Action: `analyze_new_note`
  - Intent: Auto-analyze each new entry right away (vs. nightly batch)
  - Config: enabled flag (system toggle from SystemPage)
  - Produces: AI analysis sidecar + image attachment folding
  - Risk: **MED** — latency per entry (model call). Cost per note if enabled. Opt-in via system toggle.

- **analyze-notes** (NOT in workflows/, likely nightly server job)
  - Action: `analyze_notes` (2089 bytes in actions/)
  - Intent: Batch AI analysis of unanalyzed notes
  - Config: limit (max notes per run)
  - Risk: **MED** — watermark-based to avoid re-analyzing

- **title-notes** (ACTIVE, likely nightly)
  - Action: `title_notes`
  - Intent: Auto-generate titles for untitled notes
  - Risk: **LOW** — idempotent (checks for existing title)

- **generate_tags** (ACTIVE, event-driven or batch)
  - Action: `generate_tags`
  - Intent: AI tag suggestions for notes
  - Risk: **LOW** — additive (never deletes user tags)

#### Cleanup & Filing
- **sort-unfiled** (ACTIVE, likely on-demand or nightly)
  - Action: `sort_unfiled`
  - Intent: File undated/unorganized notes into the dated tree (notes/daily/YYYY/MM/DD/<n>)
  - Risk: **MED** — date extraction from note content could be wrong; human review of proposals recommended

- **redate-notes** (ACTIVE, config-dependent)
  - Action: `redate_notes` (1773 bytes)
  - Intent: Re-date notes based on derived timestamps (e.g., from calendar events, location data)
  - Risk: **MED** — retroactive date changes can break chronological assumptions elsewhere

#### Medical/Health Domain
- **extract-events** (ACTIVE, likely on medical notes)
  - Action: `extract_events` (2227 bytes)
  - Intent: Pull calendar events from medical notes (appointments, lab tests, procedures)
  - Produces: Calendar entries (kind='appointment', 'deadline', etc.)
  - Risk: **MED** — extraction could miss edge cases; human review recommended

- **extract_health** (kb maintenance, part of wiki_extract_health)
  - Intent: Synthesize health-domain KB articles (conditions, medications, procedures)
  - Risk: **LOW** — read-only KB assembly

#### Financial & Accounting
- No dedicated workflows in the set; financial notes are captured to notes/financial/ folder, then handled by general analysis.

#### Research & External Lookup
- **research-nudge** (ACTIVE, manual-or-scheduled)
  - Action: `research_candidate_nudge`
  - Intent: Flag under-researched notes + prompt for external lookup
  - Produces: Review cards + approval requests (awaiting user decision in Research mode)
  - Risk: **MED** — nudge frequency could spam the inbox if not rate-limited

#### Location & People
- **discover-places** (ACTIVE, manual-or-scheduled)
  - Action: `discover_places` (1511 bytes)
  - Intent: Find frequent location clusters (dwell points) → suggest saved places
  - Produces: Place creation prompts
  - Risk: **LOW** — read-only detection; user approves place saves

- **location_notify** (ACTIVE, config-dependent)
  - Action: `location_notify` (1150 bytes)
  - Intent: Notify owner when crossing geofence boundaries (location alerts)
  - Risk: **MED** — geofence boundaries must be clearly defined; false positives (boundary oscillation) could spam

#### Recurrence & Patterns
- **promote-recurrences** (detailed above)
- **promote_reference_candidates** (ACTIVE, likely KB-driven)
  - Action: `promote_reference_candidates`
  - Intent: Elevate under-cited chatter notes to KB references
  - Risk: **MED** — citation scoring could have false positives

#### Kb Coverage & Audit
- **kb-coverage-check** (ACTIVE, manual-or-scheduled)
  - Action: `kb_coverage_check` (3048 bytes in actions/)
  - Intent: Audit KB for gaps, dead links, stale references
  - Produces: Review cards flagging problems
  - Risk: **LOW** — read-only audit

- **audit_link_labels** (ACTIVE, likely weekly)
  - Action: `audit_link_labels` (1932 bytes)
  - Intent: Check for stale wiki link labels (e.g., [[Target|OldName]])
  - Produces: Repair suggestions; can auto-fix via LinkAuditPanel in Wiki.tsx
  - Risk: **LOW** — repairs are safe (deterministic label update)

#### Misc
- **refresh-reference-seeds** (ACTIVE, on-demand)
  - Action: `refresh_reference_seeds`
  - Intent: Re-seed KB reference index (for external lookup)
  - Risk: **LOW** — idempotent rebuild

- **summarize_day_log** (ACTIVE, likely post-consolidate)
  - Action: `summarize_day_log` (2287 bytes)
  - Intent: Create a high-level day summary for calendar/agenda view
  - Risk: **LOW** — summary-only, non-destructive

- **synthesize** (ACTIVE, likely nightly)
  - Action: `synthesize` (1933 bytes)
  - Intent: General KB synthesis pass (gather new insights, draft new articles)
  - Risk: **MED** — LLM-driven; quality depends on prompt + corpus

- **synthesize_wiki** (ACTIVE, KB-driven)
  - Action: `synthesize_wiki` (3382 bytes)
  - Intent: Deep synthesis of KB structure + cross-references
  - Risk: **MED** — comprehensive; could produce contradictions if not carefully guided

### Prompts.yaml (110KB)

The single source of truth for agent configuration:

```yaml
agent:
  model: ""              # blank => env LLM_MODEL
  max_tokens: 2048       # per turn
  max_iterations: 8      # tool loops per reply
  max_total_tokens: 60000  # cumulative budget
modes:
  assisted: [tools]        # Socratic thinking partner
  research: [tools]        # Read-only assistant
  full: [tools]            # Write + propose + edit
  entry: n/a               # No AI
models:
  default: ""              # Chat agent fallback
  cheap: "claude-haiku-4-5-20251001"  # Tags, summaries, filing
  synthesis: ""            # KB synthesis (set Opus id for quality)
  vision: ""               # Image analysis
```

**Risk**: **MED** — Prompts define agent behavior. Injection risk if user-supplied text leaks in. Tool descriptions are auto-used by the LLM; typos or ambiguities could cause misbehavior.

---

## 3. ANDROID APP (android/)

### Phone App (android/app/src/main/)

#### Capture Pathways (CaptureActivity.kt)
- **Intent**: One-tap dictation/photo capture via home-screen widgets
- **Observable behaviors**:
  - Widget tap → CaptureActivity (translucent, invisible)
  - ACTION_RECOGNIZE_SPEECH (system speech recognizer) → dictation text
  - ACTION_IMAGE_CAPTURE (system camera app) → JPEG file
  - Results handed to UploadWorker for async upload (retries on network failure)
- **Risk**: **MED** — speech recognizer app may not be available on all devices. Camera app crashes are handled (empty photo deleted) but not reported.

#### Note Relay Service (NoteRelayService.kt)
- **Intent**: Forward watch dictations to JBrain; relay acknowledgments back to watch
- **Observable behaviors**:
  - WearableListenerService listens for watch notes (message at NOTE_PATH)
  - Calls NoteClient.createEntry(text) (async forward to JBrain)
  - If success: NoteQueue.flush() (drain queued notes); else enqueue for retry
  - Acknowledges watch with "ok" or "err:<reason>" (drives wrist UI)
  - Posts notification with result (visible even if app is closed)
  - Records RelayLog entry (shown on phone screen)
- **Risk**: **MED** — relies on Google Play Services (Wear Data Layer). If watch is unreachable, relay hangs until timeout. Queue backlog could grow unbounded if server is down.

#### Location Service (LocationService.kt)
- **Intent**: Foreground service that streams GPS location to JBrain in background (keeps running even when app is closed)
- **Observable behaviors**:
  - Smart polling: asks Activity Recognition for still ↔ moving transitions
  - While MOVING: continuous high-accuracy GPS fixes
  - While STILL: low-frequency heartbeat (GPS off)
  - Buffers fixes offline; flushes to /api/locations/bulk in batches (periodic + on-demand)
  - Server applies keep-rule (device just forwards); per-person attribution via "source" field
  - No activity permission → falls back to always-on continuous GPS
- **Risk**: **HIGH** — foreground service must have persistent notification (battery drain risk if user doesn't know it's running). GPS data is privacy-sensitive; escapes to shared location links. Fallback (always-on GPS) has huge battery cost.

#### Photo/Text Upload (UploadWorker.kt)
- **Intent**: Durable background upload of captures (text or photo); survives app kills, reboots, network failures
- **Observable behaviors**:
  - WorkManager (retries with exponential backoff, requires CONNECTED network)
  - Text mode: POST createEntry(text, source='user') → note slug
  - Photo mode: POST createEntry("📷 Photo") → get slug, then POST attachment (JPEG)
  - Slug cache per work-id: retries re-attach to same note (no duplicate notes on retry)
  - Notifications on success/pending
  - Never loses captures (queued until landed)
- **Risk**: **LOW** — WorkManager is robust. Slug cache could stale if work-id table is cleared; edge case.

#### Settings & Configuration (Settings.kt)
- **Intent**: Persist app configuration (server URL, API key, LLM model choice, location tracking toggle, etc.)
- **Observable behaviors**:
  - SharedPreferences storage (device-local, survives app update)
  - Editable fields: server URL, API key, location tracking enable/disable
- **Risk**: **HIGH** — API key stored in SharedPreferences (unencrypted on most devices). Use EncryptedSharedPreferences for production.

### Watch App (android/wear/src/main/)

#### Watch UI & Dictation (MainActivity.kt, CaptureTile.kt)
- **Intent**: Quick dictation interface + tile shortcut for note capture
- **Observable behaviors**:
  - CaptureTile: one-tap transcription (ACTION_RECOGNIZE_SPEECH on watch speaker/mic)
  - MainActivity: minimal UI (show captured text, retry/send buttons)
  - Send → relays text to phone via NOTE_PATH message (NoteRelayService picks it up)
  - Tile shortcut for easy access from watch face
- **Risk**: **LOW** — watch-side is thin; phone relay is the critical path.

#### Phone Relay (PhoneRelay.kt)
- **Intent**: Communicate with phone over Wear Data Layer (request/response messaging)
- **Observable behaviors**:
  - Listens for acknowledgments from phone (RESULT_PATH messages)
  - Shows ACK/NAK result to user ("✓ saved" vs "✗ failed: <reason>")
  - Updates UI on watch based on phone feedback
- **Risk**: **MED** — if phone relay service isn't running, watch gets no ack (timeout → "failed" UI). User must re-sync.

#### Note Queue (NoteQueue.kt on watch)
- **Intent**: Buffer unpaired/offline dictations on watch (WearOS has no persistent storage, so this is limited to RAM)
- **Observable behaviors**:
  - Queue notes while phone is disconnected or offline
  - Flush to phone when link restores
- **Risk**: **HIGH** — WearOS kills background services aggressively. Queue held in RAM (lost on app kill). Dictations on watch may not survive app restart. No persistence layer.

---

## Risk Summary

### Highest Risk Areas (require most testing)

1. **Chat mode auth & escalation** (Entry → Research → Full Brain)
   - Mode switching logic; fallback to research on sessionStorage stale
   - External lookup approval UX (missing approval could cause dangerous LLM actions)

2. **Note editing & version history**
   - Simultaneous edits from two tabs (no server-side locking)
   - MarkdownDiff rendering on large documents
   - Redirect handling (merged notes must not create cycles)

3. **Encryption in Shares**
   - Web Crypto API AES-GCM implementation
   - Key derivation from token (PBKDF2?)
   - PII in encrypted transcripts (storage + deletion)

4. **Database Backup & Restore**
   - Restore is DESTRUCTIVE; must have explicit confirmation
   - No undo after restart
   - SQL injection surface (server-side validation)

5. **Location Tracking**
   - GPS data privacy (stored server-side, shared on public links)
   - Device-bound share links (binding enforcement)
   - LocationService foreground notification (battery/privacy warning)
   - iOS vs Android API differences

6. **Workflow Execution**
   - Cron-scheduled workflows (timezone handling: server TZ vs owner TZ)
   - Manual "Run now" debouncing (prevent double-click triggering two runs)
   - Live watch polling (could miss fast steps; lag on network loss)
   - Pipeline step labels (STEP_LABELS dict must stay in sync with server primitives)
   - Watermark-based idempotency (restart backfill could re-run completed steps)

7. **Android Configuration**
   - API key stored unencrypted in SharedPreferences
   - Watch app queue lost on app kill (no persistence)
   - NoteRelayService timeout handling

### Medium-Risk Areas

- Calendar TZ handling (server TZ vs owner TZ vs browser TZ)
- Search semantic-mode fallback (warmup detection reliability)
- Map performance (Douglas–Peucker simplification, vertex cap, zoom-adaptive epsilon)
- Lab analytes time-window persistence (localStorage, best-effort)
- Update detection polling (15s + 90s fail threshold)
- Entity clustering for recurrence detection (false positives)
- Link audit auto-fix determinism

### Low-Risk Areas (well-isolated, read-only)

- Wiki browser & note view
- Entity browser & knowledge graph
- Calendar reminders (read-only surface)
- Backup export (read-only)
- Review inbox (simple list)
- Shares (standard auth patterns)

---

## Testing Priorities

### Untested/Under-tested Surfaces

1. **UI Flows** (manual, event-driven):
   - Mode switching & mode persistence across sessions
   - Share link creation + recipient access + device binding reset
   - Note editing (concurrent edits from multiple tabs)
   - Attachment upload + medical auto-analyze
   - Calendar event creation + reminder triggers
   - Guided share workflow (conversational flow, transcription storage)
   - Encrypted chat (key derivation, decryption, message order)

2. **Cron Workflows** (time-dependent, hard to trigger in tests):
   - daily-consolidate (midnight, TZ-specific)
   - calendar-reminders (7am, TZ-specific)
   - wiki-maintain (incremental updates, watermark logic)
   - Restart backfill (idempotency checks)

3. **Android Integration**:
   - Watch relay (Wear Data Layer pairing)
   - LocationService (smart polling, dwell detection)
   - UploadWorker retries (network failure scenarios)
   - Settings persistence (config round-trip)
   - Photo capture via system camera (JPEG handling)
   - Speech recognition (no recognizer available)

4. **Edge Cases**:
   - Very large notes (>10MB, >100k lines) rendering
   - Redirect cycles (note A → B → A)
   - Deleted notes referenced in shares
   - Concurrent workflow runs (wiki-build + wiki-maintain both triggering)
   - Server restart during update check
   - Offline-first scenarios (app closed, PWA cache, then reconnect)

---

## Feature Completeness

### Core Compose
- [x] Entry (write-only) with 3 sub-types
- [x] Research (read-only, assistant)
- [x] Full Brain (write + propose + edit)
- [x] Mode persistence + legacy remapping
- [x] Staging panel + undo

### Wiki & Knowledge Base
- [x] Wiki browser (tree, filters, collapse logic)
- [x] Note page (edit, history, diff, attachments, share)
- [x] KB rebuild (wiki-build workflow)
- [x] KB maintenance (incremental updates)
- [x] Citation audit (link label fixes)
- [x] AI rebuild for KB articles

### Search
- [x] Hybrid (keyword + semantic)
- [x] Keyword (BM25)
- [x] Semantic (embeddings, with fallback)
- [x] Entities (entity index lookup)
- [x] Result relevance scoring

### Shares & Collaboration
- [x] Note share links (TTL, device binding, editable)
- [x] Guided interviews (goal-driven, conversational)
- [x] Encrypted peer chat (E2E, Web Crypto)
- [x] Research links (article + citations)
- [x] Lab chart shares

### Medical/Health
- [x] Lab tracking (analytes, time windows, charts)
- [x] Medical note capture (to notes/medical/)
- [x] Event extraction (appointments, lab tests)
- [x] Health-domain KB synthesis

### Location & People
- [x] Location tracking (trail, heatmap, device binding)
- [x] Places (save, update, delete, geofence alerts)
- [x] People (label, colour, location keys)
- [x] Setup code (jbt1 format for device config)
- [x] Smart polling (still ↔ moving transitions)

### Calendar
- [x] Event creation & editing (kind, reminder presets)
- [x] Views (list, day, week, month)
- [x] Reminders (timed + all-day presets)
- [x] Event dismissal

### Automation
- [x] Workflow scheduling (cron, interval, event)
- [x] Manual "Run now" trigger
- [x] Live pipeline watch (step status)
- [x] Config editor (per-workflow tuning)
- [x] 20+ actions (consolidate, wiki-build, analyze, tag, file, promote, etc.)

### System & Settings
- [x] Update trigger & progress monitoring
- [x] Settings (owner, TZ, LLM model, voice, location, notifications)
- [x] Prompts editor (live edit of system prompts)
- [x] Database backup & restore
- [x] SQL console (free-form query)

### Android
- [x] Phone dictation + photo capture (widgets)
- [x] Watch dictation + relay (Wear Data Layer)
- [x] Location tracking (foreground service, smart polling)
- [x] Background upload (WorkManager)
- [x] Settings persistence

