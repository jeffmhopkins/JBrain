# JBrain Backend Services Feature Inventory

**Audit Date:** 2026-06-08  
**Scope:** 62 service modules, ~21K SLOC (server/app/services/)  
**Methodology:** Read-only analysis of service module docstrings, function signatures, and domain logic.

---

## Executive Summary

JBrain is a sophisticated **personal-knowledge self-hosted wiki** with AI-assisted maintenance. The backend combines:
- **Ingestion pipelines** (notes, labs, attachments, audio/video, images)
- **Analysis sidecars** (entity extraction, calendar events, lab results, note summaries)
- **LLM-powered tools** (architect agent, wiki synthesis, medical references, share-link AIs)
- **Search/retrieval** (semantic + keyword hybrid, FTS, embeddings)
- **Sharing primitives** (encrypted chat, guided intake, research links, lab exports)
- **Maintenance workflows** (entity rebuilds, wiki maintenance, reference promotion)

**Risk Profile:**
- **HIGH RISK:** LLM-dependent synthesis, lab data ingestion (medical), encryption, entity identity decisions
- **MED RISK:** Attachment parsing (PDFs, images, audio), entity deduplication heuristics, calendar supersession
- **LOW RISK:** Pure utilities (clock, geo, diffing, FTS), read-only tools, deterministic parsing

**Hardest to Test:**
1. Architect agent tool loop (LLM-dependent, streaming, many tools)
2. Wiki synthesis pipeline (multi-stage LLM, requires quality judges)
3. Lab PDF/image parsing + vision OCR (geometry-aware, variable formats)
4. Entity merging heuristics (probabilistic name matching, merge-chain resolution)

---

## Service Clusters

### 1. CORE NOTE PIPELINE (4 services)

#### `notes.py` — Note write pipeline [HIGH RISK]
- **Purpose:** Centralized note create/update/delete with versioning, FTS, embeddings, wikilinks.
- **Key Functions:**
  - `upsert_note()` (line:80+) — create/update with versioning, embedding sync, FTS, fire events
  - `_rename_inbound_links()` (line:66+) — rewrite [[...]] links when titles change
  - `set_tags()` — manage note tags
  - `delete_note()` — soft-delete + cascade (links, embeddings, analysis sidecars)
- **Externally Observable:**
  - Version limit (MAX_VERSIONS_PER_NOTE=50) prevents runaway growth
  - Entry-event workflow firing (deferred, uses thread-local suppression guard)
  - Embedding async/sync coordination (write lock held during embed call — risk of blocking)
- **Edge Cases / Failures:**
  - Recursive re-fire suppression (entry_created events must not recurse)
  - Large note embedding may block other writers (embedding outside lock preferred but not done here)
  - Rename cascade: orphaned backlinks if a title rename race-conditions
- **Risk:** MEDIUM — data mutation, versioning correctness

#### `note_analysis.py` — Cached per-note AI analysis [HIGH RISK]
- **Purpose:** One-shot AI extraction (gist, facts, dates, entities) cached by content hash.
- **Key Functions:**
  - `get()` (line:~80) — fetch cached analysis or None
  - `request_fold()` (line:~300+) — coalesced async analysis request
  - `set()` — store analysis result (gist, facts, dates, entities_json)
- **Externally Observable:**
  - Content-hash keying (same content → same analysis, even across renames)
  - Coalescing via "dirty flag + background worker" pattern (like entity_rebuild)
  - LLM call: cheap model, structured extraction, ~6000 tokens max input
  - Falls back to content snippet if analysis fails
- **Edge Cases / Failures:**
  - Stale pending rows (reset_stale on boot, shared with image_analysis)
  - Image/audio attachment re-analysis triggers note re-fold
  - Entity extraction → entity_index.rebuild dependency
- **Risk:** HIGH — LLM-dependent correctness, used by wiki pipeline and entity clustering

#### `note_normalize.py` — Bulk note-title normalization
- **Purpose:** Tidy loose entry notes into flat dated tree (notes/YYYY/MM/DD/N).
- **Key Functions:**
  - `redate_batch()` — deterministic per-day renaming, no LLM
  - `list_noncompliant()` — find notes outside the standard tree
- **Externally Observable:**
  - Deterministic, preserves entry order within a day
  - Re-runs safely (idempotent by new_title + old_title check)
- **Risk:** LOW — deterministic, no data loss

#### `attachments.py` — Attachment write pipeline [MEDIUM RISK]
- **Purpose:** Store files + extract text (PDFs, images, code), chunk for embeddings, index FTS.
- **Key Functions:**
  - `add_attachment()` (line:202+) — ingest, dedup by SHA256, extract text, chunk & embed
  - `chunk_text()` (line:173+) — markdown-aware chunking with overlap, max 1200 chars/chunk
  - `extract_text()` (line:86+) — PDF text extract, image EXIF, code/text decode
  - `context_block_for_note()` — bounded digest for LLM (transcripts, image summaries, PDFs)
- **Externally Observable:**
  - Deduplication by SHA256 + note_id (same file twice on same note = one row)
  - MAX_ATTACHMENT_BYTES=100MB, MAX_CHUNKS=300
  - PDF text capped at 200K chars
  - Chunking uses sentence/paragraph boundaries, headers become breadcrumbs
  - Image EXIF extraction (safe: metadata only, no pixels stored)
- **Edge Cases / Failures:**
  - PDF with no text layer → routed to lab_vision / image_analysis
  - Large PDF (>200K text) silently truncated; no warning
  - Image analysis sidecar deletion cascades to note (if analysis_md present)
- **Risk:** MEDIUM — external data (PDFs, images), text extraction quality, truncation silent

---

### 2. MEDIA ANALYSIS (3 services)

#### `audio_transcription.py` — Local speech-to-text [MEDIUM RISK]
- **Purpose:** Whisper-based transcription of audio/video attachments (local, no API key).
- **Key Functions:**
  - `start_transcription()` (line:378+) — async worker spawn, guard double-runs, mark pending
  - `transcribe()` (line:276+) — worker thread (Whisper model load + run)
  - `_extract_frames()` (line:198+) — video frame sampling (PyAV), adaptive cadence
- **Externally Observable:**
  - Per-video: frame extraction at 25% interval (or time interval, capped to 30 max)
  - Transcript stored as analysis_md sidecar (same slot as image vision)
  - Visual summary (LLM-optional, best-effort)
  - Embeds searchable content + triggers note_analysis.request_fold
  - Stale 'pending' rows reset on boot (shared watchdog with image_analysis)
- **Edge Cases / Failures:**
  - Embedding done BEFORE write lock to avoid blocking WAL single writer
  - Visual summary is best-effort: fails silently if no LLM key
  - Non-decodable video → [] frames, transcript only
  - Whisper model download on first use (can be slow, network-dependent)
- **Risk:** MEDIUM — LLM optional, Whisper model size/latency, frame extraction quality

#### `image_analysis.py` — Vision-based image + scanned-PDF analysis [MEDIUM RISK]
- **Purpose:** Claude vision API → structured image summary + salient facts.
- **Key Functions:**
  - `analyze()` (line:~400+) — spawn async worker, mark pending
  - `vision_summary_frames()` (line:~200+) — batch frame analysis via vision LLM
  - `strip_summary_block()` — remove analysis_md from note on attachment delete
  - `render_pdf_to_images()` — PDF → image array (pypdfium2), for scanned PDFs
- **Externally Observable:**
  - Per-attachment state: pending → done/error (analysis_md sidecar)
  - Vision summary + detected facts + locations (if present)
  - Capped output (1500 chars per attachment)
  - Blocks vision calls until model is ready (lazy load, cached for process)
  - Scanned PDF → image render → vision → lab_parse attempt
- **Edge Cases / Failures:**
  - Vision model unavailable → analysis error (surfaced to user)
  - PDF render unavailable (no pypdfium2) → 'image_unparsed' state (visible error)
  - Stale 'pending' rows after crash (reset_on_boot, shared watchdog)
  - Re-analyze is force-able (owner can restart wedged analysis)
- **Risk:** MEDIUM — LLM-dependent, external vision API, model availability, PDF parsing

#### `lab_vision.py` — OCR-gated lab-result photo/scanned-PDF analysis [MEDIUM-HIGH RISK]
- **Purpose:** Vision + deterministic parsing for unstructured lab photos/screenshots.
- **Key Functions:**
  - `parse_lab_image()` (line:~150+) — vision → structured lab results
  - `render_pdf_to_images()` — scanned PDF → image frames
  - Field extraction: patient identity, test name, result values, dates, units, ranges
  - Faithfulness check: extracted value must appear in document text (confidence guard)
- **Externally Observable:**
  - Doctor's note photo / patient portal screenshot → lab values
  - Identity extraction (name, DOB, MRN; matched against configured owner)
  - Confidence scores per result
  - Skips: reasons for unparsed fields (displayed to owner)
- **Edge Cases / Failures:**
  - Vision hallucination risk (values that never appeared in document) — mitigated by faithfulness check
  - Identity mismatch warning: DOB differs from owner → "wrong patient" alert
  - Portrait vs landscape orientation, varied fonts, poor lighting
  - Date parsing ambiguity (locale, format variation)
- **Risk:** HIGH — medical data, vision accuracy, OCR identity verification, hallucination guard

---

### 3. LAB INGESTION & ANALYSIS (4 services) [HIGH RISK]

#### `lab_parse.py` — Deterministic lab PDF parsing
- **Purpose:** Geometry-aware extraction from "Result Trends" transposed matrices + standard reports.
- **Key Functions:**
  - `parse_lab_pdf()` (line:~350+) — pdfplumber coordinates → analyte/date alignment
  - `analyte_key()` (line:63+) — normalize "white blood cell" → "wbc" (CBC canonical keys)
  - `parse_date()` (line:86+) — "Jul 3, 2022" → ISO date validation
  - Reference range extraction: "Normal Range: 3 - 10 mg/dL" → (3, 10, "mg/dL")
- **Externally Observable:**
  - Column-anchored by x-coordinate (date headers)
  - Row-anchored by "Normal Range:" line (analyte name, unit, ref range)
  - Numeric values mapped to (analyte by y-band, date by x-anchor)
  - Handles multi-row analytes (lab changed format mid-history)
  - Faithfulness: values only if exact text appears in PDF
- **Edge Cases / Failures:**
  - Day/month ambiguity: "3/7/2022" resolved via context (> 12 suggests day)
  - Reference ranges: "<3", "≤10", "10 - 20" all supported
  - Transposed matrix assumes headers at top; standard reports may fail
  - Scanned PDFs (no text layer) → routed to lab_vision
- **Risk:** MEDIUM — deterministic but geometry-dependent, format-specific, date parsing

#### `lab_ingest.py` — Staged lab PDF approval workflow [HIGH RISK]
- **Purpose:** Extract → preview → owner approve pipeline (mirroring image_analysis).
- **Key Functions:**
  - `_extract()` (line:72+) — picks text PDF or vision path
  - `stage_from_attachment()` — populate lab_ingestion_staged table
  - `approve_staged()` — idempotent move staged → lab_results (INSERT OR IGNORE per identity_key)
  - `_identity_state()` (line:44+) — three-state patient verification (match/unverified/mismatch)
- **Externally Observable:**
  - Deterministic identity_key = SHA256(analyte|date|value|unit|source_pdf_sha)
  - Patient DOB verification (owner config vs document)
  - Staged preview: document type, parsed results, confidence, skips (with reasons)
  - Approval is idempotent (re-approve same attachment = no dupes)
  - Status lifecycle: extracted → approved → error
- **Edge Cases / Failures:**
  - Owner DOB set, document DOB missing → "unverified" (never assume match)
  - Owner DOB mismatch → loud warning, staged but not approved by default
  - Scanned/unparseable PDFs flagged as 'image_unparsed' (visible to owner)
  - Faithfulness check: value must exist in document text (blocks hallucinations)
- **Risk:** HIGH — medical data, patient identity verification, approval is final (no audit trail stored)

#### `lab_series.py` — Read-only lab-trend builders
- **Purpose:** Chart builders for semantic search + graph render.
- **Key Functions:**
  - `analyte_picklist()` — alphabetical by display name (not key)
  - `single_analyte_series()` — sorted by date, encounters, overlapping values per unit
  - `reference_bands()` — visual reference range segments (low/normal/high)
  - Unit unification: reject extremes across non-equivalent units
- **Externally Observable:**
  - Picklist caching (generated fresh per call, no persistence)
  - Multi-unit analytes: each unit tracked separately, extremes never cross units
  - Encounters (lab test sessions) grouped, values within encounter deduplicated
  - Reference bands: smooth segments, handle missing/partial ranges
- **Edge Cases / Failures:**
  - No reference range → upper/lower omitted, values still charted
  - One value per unit/date/encounter (later approval replaces earlier)
  - Unit mismatch (e.g., mg/dL vs mmol/L): values kept separate
- **Risk:** LOW — read-only, used for display only

#### `lab_share_scope.py` — Lab-share recipient security boundary [HIGH RISK]
- **Purpose:** Enforce analyte allow-list + date window + identity-strip for shared labs.
- **Key Functions:**
  - `scoped_query()` — apply analyte ALLOW-LIST + date clamps + identity-strip
  - `identity_stripped()` — remove patient name, DOB, MRN (all PII)
  - Permission checks: is this recipient allowed?
- **Externally Observable:**
  - Owner-approved analyte list (whitelist enforcement)
  - Date window clamp (lower/upper ISO bounds)
  - Identity fields nulled (never exposed to recipient)
  - Query audited per recipient session
- **Edge Cases / Failures:**
  - Edge case: analyte in allow-list but no results in window → empty chart (correct)
  - Date boundaries: inclusive on both ends
  - Identity-strip must be complete (no leakage via analyst names, locations, etc.)
- **Risk:** HIGH — security boundary, PII protection, recipient access control

---

### 4. ENTITY SYSTEM (4 services) [MEDIUM-HIGH RISK]

#### `entity_index.py` — Canonical entity index (derived) [MEDIUM RISK]
- **Purpose:** Merge person/org/place name variants heuristically + user overrides.
- **Key Functions:**
  - `rebuild()` (line:~600+) — re-derive from note_analysis entities + user decisions
  - `normalize()` (line:38+) — lower, strip titles/punct, token-join
  - `_merge_map()` (line:89+) — union-find: subset-name variants + forced merges
  - Merge rule: same type + token-set subset + shared distinctive (3-letter) token
- **Externally Observable:**
  - Merges (heuristic + user-forced), splits (blocked pairs), aliases (extra names)
  - Surname-first matching for acronyms (e.g., "Bob" ↔ "Robert Smith")
  - Nickname lexicon fold (Bob ↔ Robert via static dictionary)
  - Owner-alias folds ("me", "owner", "self" → owner's canonical person)
  - Deduplication: merges are IDEMPOTENT (re-run same decision = stable result)
- **Edge Cases / Failures:**
  - Split enforcement: prevents transitively co-grouping across split boundaries
  - Forced merges can bind even when one side is absent (dormant merges)
  - Heuristic merges never override splits (split wins)
  - Very long names or initials-only names may cluster poorly
- **Risk:** MEDIUM — probabilistic heuristics (might wrongly merge unrelated Johns), user control present

#### `entity_decisions.py` — Durable entity identity decisions
- **Purpose:** Append-only ledger (merge/split/alias) that survives rebuilds.
- **Key Functions:**
  - `add()` (line:24+) — normalize keys, enforce mutual exclusion (merge ↔ split cancel), dedup
  - `load_merges()` (line:75+) — resolve chains (X→Y, Y→Z → X→Z terminal)
  - Mutual exclusion: merge on a pair deletes prior split, and vice-versa
- **Externally Observable:**
  - Normalized keys (before storage)
  - Chain resolution for merges (terminal canonical computed)
  - Idempotent dedup (same decision added twice = returns existing id)
- **Risk:** LOW — append-only, dedup ensures idempotence

#### `entity_rebuild.py` — Deferred, coalesced entity rebuilds [MEDIUM RISK]
- **Purpose:** Coalesce rapid entity-merge clicks → at most one background rebuild at a time.
- **Key Functions:**
  - `request_rebuild()` (line:~140+) — set dirty flag + spawn worker if idle, or set pending
  - `_rebuild_pass()` (line:68+) — clear dirty flag, run entity_index.rebuild(), bump generation
  - Worker: own thread-local DB connection, drains pending queue
- **Externally Observable:**
  - Durable "dirty" flag (survives crash; reconciled on boot / next decision)
  - Coalescing: burst of clicks → max one extra pass after current finishes
  - UI poll target: `status()` returns rebuilding/idle + generation (completion signal)
  - No silent failures (error → status='error', decision remains durable)
- **Edge Cases / Failures:**
  - Rebuild raises → dirty flag stays set, next decision or scheduler reconciles
  - Worker never crashes process (exception caught, logged, retry on next trigger)
- **Risk:** MEDIUM — background thread coordination, durable state + process-local flags

---

### 5. WIKI BUILD PIPELINE (6 services) [HIGH RISK]

#### `wiki_build.py` — KB build engine (reset + outline + write) [HIGH RISK]
- **Purpose:** Multi-stage auto-rebuild: reset → corpus survey → outline (LLM) → write articles (LLM).
- **Key Functions:**
  - `reset()` (line:~100+) — soft-delete kb/ articles (except kb/_*), clear synthesis watermark
  - `corpus_digest()` — compact survey: gist + domain + entities per note
  - `outline()` (line:~300+) — LLM: entity-first taxonomy, scope, assignments
  - `write_batch()` (line:~500+) — LLM: write each article, lint once, save if ok
  - `write_one()` (line:~350+) — write ONE article from sources + domain guide
  - Continuation: auto-continue on truncation (stage 1), retry at bigger cap (batch, then fail)
- **Externally Observable:**
  - Source-of-truth: raw notes stay ground truth, articles re-derivable
  - Scope per article: the note ids assigned to it (owner-curated before write)
  - Domain guide: context per kb/<domain> (customizable prompts)
  - Lint checks: stub detection (ok=true/false), structure validation
  - Truncation handling: streaming auto-continues (stage 1 + live rebuild), batch retries then quarantines
- **Edge Cases / Failures:**
  - Article moved between domains → old domain loses it (sweep depends on scope)
  - Truncated article at token cap → ok=false (must be reviewed before save)
  - Lint fails: article quarantined (staging_actions CREATE not saved)
  - Domain guide missing → use default system prompt
- **Risk:** HIGH — LLM-synthesized content, quality depends on model, truncation recovery

#### `rebuild_engine.py` — Two-stage "Rebuild page now" engine [HIGH RISK]
- **Purpose:** Live, owner-curated rebuild (gather sources → draft article).
- **Key Functions:**
  - Stage 1 — `gather()` (line:~100+) — tool-driven agent, cheap model, streams tool calls
    - Seeds from: prior citations ∪ entity index
    - Searches for more sources
    - Proposes candidate set with reason per item
  - Stage 2 — `draft()` (line:~200+) — tool-less synthesis, writes from curated sources
    - Thinking model (reasoning visible)
    - Adaptive token budget (default 6k, re-draft can grow to 16k)
    - Streaming draft + reasoning
  - State tracking: rebuild_runs (in-memory, session-only)
- **Externally Observable:**
  - Separate transcripts (gather has tools, draft has thinking, no crossing)
  - Guide can resume draft from same loaded context
  - Safeguard: content-hash check on Accept (staleness guard)
  - Never touches live note until Accept
- **Edge Cases / Failures:**
  - Truncated draft: "Re-draft with more room" option (user-approved budget increase)
  - Stale page: Accept rejected if note changed since gather started
  - Session TTL: idle runs reaped
- **Risk:** HIGH — LLM-dependent, two-stage design (gather can fail, draft can truncate)

#### `rebuild_runs.py` — In-memory rebuild session registry
- **Purpose:** Live session tracking (no DB persistence).
- **Key Functions:**
  - `new_run()` — allocate opaque run_id, store provider messages + draft + content hash
  - `get_run()` — fetch session state
  - `extend_ttl()` — sliding window (idle runs reaped)
- **Externally Observable:**
  - Session only (not persisted across restart)
  - Content hash (staleness check at Accept)
  - Provider message blocks verbatim (for resuming draft)
- **Risk:** LOW — session-only, no persistence

#### `wiki_guides.py` — Domain-guide builders + prompts [MEDIUM RISK]
- **Purpose:** Context per kb/<Domain> for article writers.
- **Key Functions:**
  - `entity_guide()` — sample entities in domain (context for writer)
  - `scope_guide()` — note titles + snippets (what's in scope)
- **Externally Observable:**
  - Entity examples (representative entities in domain)
  - Scope preview (abbreviated note list)
  - Capped for token budget
- **Risk:** LOW — read-only context building

#### `wikilinks.py` — [[wikilink]] parsing + resolution [LOW RISK]
- **Purpose:** Parse [[Title]] + |display aliases, resolve to note ids.
- **Key Functions:**
  - `parse_links()` — extract [[...]] with regex
  - `resolve()` — title → note id (case-insensitive, path-aware)
  - Wikilink variants: [[Title]], [[Title|display]], [[kb/Title]], [[notes/path/to/Title]]
- **Risk:** LOW — deterministic parsing

#### `article_talk.py` — Per-article talk (maintenance memory) [MEDIUM RISK]
- **Purpose:** Wikipedia-Talk-style: decision log, conflicts, questions, directives, notes.
- **Key Functions:**
  - `record()` (line:57+) — batch add entries (dedup on normalized body)
  - `open_for()` — unresolved entries (what maintenance loop should act on)
  - `demote_stub_notes()` (line:111+) — reclassify "stub/needs more sources" to inert
- **Externally Observable:**
  - Kinds: decision, conflict, question, todo, directive, note, correction
  - Open kinds (actionable): conflict, question, todo, directive, correction
  - Dedup: log entries (note/decision) dedup against ALL history; actionable entries dedup against OPEN only
  - Note capping: keep at most 6 OPEN, ai-authored 'note' rows per article (clutter bound)
- **Edge Cases / Failures:**
  - Re-emerged issues (a conflict resolved, then re-surfaces) can re-add
  - Owner replies prevent deletion (cascade-delete guarded)
  - Stub-like todos demoted to inert logs (don't nag maintenance)
- **Risk:** MEDIUM — dedup logic (normalized body matching), note capping

---

### 6. REFERENCE & RESEARCH SYSTEMS (5 services)

#### `medref.py` — Drug reference linking (RxNav → MedlinePlus) [LOW RISK]
- **Purpose:** Drug name → RxNorm RxCUI → MedlinePlus consumer page.
- **Key Functions:**
  - `lookup()` — RxNav API: drug name → RxCUI
  - External link: RxNav, MedlinePlus
- **Externally Observable:**
  - Deterministic: same drug name → same page
  - External API dependency
- **Risk:** LOW — read-only, external API

#### `external_lookups.py` — Owner approval gate for external lookups [MEDIUM RISK]
- **Purpose:** Hard gate so medical_reference never sends terms without owner approval.
- **Key Functions:**
  - `request_approval()` — user sees EXACT term, approves or blocks
  - `approve()` — record approval, unlock future lookups for same term
- **Externally Observable:**
  - Approval persistence (survives session)
  - Exact-term matching (typos = separate approval request)
- **Risk:** MEDIUM — privacy boundary, must not leak PII to external services

#### `reference_candidates.py` — Topic-only candidate capture [LOW RISK]
- **Purpose:** When medical_reference surfaces a topic owner doesn't have, record for later promotion.
- **Key Functions:**
  - `record()` — capture topic + source URL + snippet (NEVER query text, dates, PII)
  - `has_reference_article()` — check if owner already has kb/Reference/<Topic>
- **Externally Observable:**
  - Privacy: ONLY public topic name + public URL + public summary (no query/context)
  - Dedup per topic
- **Risk:** LOW — data minimal, privacy-safe by design

#### `reference_promote.py` — Promote candidates → staged kb/Reference articles [MEDIUM RISK]
- **Purpose:** Nightly: topics looked up enough times → build stubs → stage for approval.
- **Key Functions:**
  - `candidates_for_promotion()` — hits >= threshold, not already in kb/Reference
  - `build_reference_stub()— deterministic from public source (topic + pinned NLM URL)
  - Stage as pending CREATE action (never auto-save)
- **Externally Observable:**
  - Hit threshold configurable
  - Deterministic: same source → same stub (idempotent stubs)
  - Provenance marker (refseed src/url/fetched) for later validation
- **Risk:** MEDIUM — external source dependency, hit counting, threshold

#### `reference_refresh.py` — Re-validate staged reference seeds [MEDIUM RISK]
- **Purpose:** Nightly: if staged seed's source is stale, re-fetch and stage an UPDATE.
- **Key Functions:**
  - `refresh_stale()` — fetched date > TTL → re-fetch, stage UPDATE
  - Preserves owner enrichments (only updates Source + refseed marker)
- **Externally Observable:**
  - TTL-driven re-fetch
  - Marker parsing: `<!-- refseed src=… url=… fetched=… -->`
  - Owner content preserved; only source line + marker refreshed
- **Risk:** MEDIUM — external API re-fetch, marker parsing, merge logic

#### `research.py` — Research-link Q&A (recipient AI, server-driven RAG) [MEDIUM-HIGH RISK]
- **Purpose:** Recipient-facing scope-bounded AI (tool-less, reads owner's brain only).
- **Key Functions:**
  - `answer()` (line:~80+) — retrieve context from scope, feed to LLM, return answer
  - Recipient can only see scope's approved_ids (no ID leakage)
  - Server-driven RAG: no model access to note ids/titles
- **Externally Observable:**
  - Injection guards: strip `<<>>`, URLs, wikilinks, jailbreak phrases from recipient input
  - Context capped (9000 chars)
  - Answer capped (500 tokens)
  - Global daily reply cap (1000, all research links) — cost backstop
  - Rate limit per IP
- **Edge Cases / Failures:**
  - Recipient tries jailbreak: cleaned input, no escalation
  - Out-of-scope question: model has no context, answers "don't know"
  - Search returns nothing: model answers from zero context (safe)
- **Risk:** MEDIUM-HIGH — recipient AI, injection guards must be watertight

---

### 7. SEARCH & RETRIEVAL (2 services)

#### `search.py` — Hybrid FTS + vector search [MEDIUM RISK]
- **Purpose:** Notes + attachments, keyword (FTS) + semantic (embeddings), fused by reciprocal rank.
- **Key Functions:**
  - `hybrid_notes()` (line:~100+) — keyword + vector, reciprocal rank fusion, cap results
  - Corpus: notes + attachment chunks + image summaries
  - Expandable search: entities in notes expanded to synonyms
- **Externally Observable:**
  - Two-path search (FTS and vector in parallel, merged)
  - RRF (reciprocal rank fusion) for tie-breaking
  - Entity expansion: "Summer Hopkins" → also find "S. Hopkins"
  - Tool-access enforcement: only notes/attachments the agent can read
- **Edge Cases / Failures:**
  - Embedding unavailable → falls back to FTS only
  - Entity expansion needs entity_index (may be stale if rebuild pending)
- **Risk:** MEDIUM — embedding quality, RRF weighting, entity expansion accuracy

#### `embeddings.py` — Local fastembed vectors [MEDIUM RISK]
- **Purpose:** 384-dim bge-small embeddings (local, no API key).
- **Key Functions:**
  - `embed_many()` — batch embed strings
  - `upsert_note_embedding()` — whole-note + chunked embeddings
  - `reindex_missing_*()` — backfill missing vectors (after migrations)
- **Externally Observable:**
  - Per-process state (SINGLE uvicorn worker; --workers would flicker health)
  - Lazy load: model on first call, cached for process lifetime
  - Readiness state: unknown → warming → ready / unavailable / failed
- **Edge Cases / Failures:**
  - Model download on first call (can be slow, network-dependent)
  - fastembed not installed → unavailable state
  - Very long text (>512 tokens) silently truncated by embedder (chunks mitigate)
- **Risk:** MEDIUM — process-local state, lazy load, silent truncation on long text

---

### 8. SHARING & COLLABORATION (8 services) [MEDIUM-HIGH RISK]

#### `share.py` — Share-link lifecycle (minting, revocation, usage) [MEDIUM RISK]
- **Purpose:** Central registry for all share-link types (view, chat, guided, research, labshare).
- **Key Functions:**
  - `create_link()` — mint token, set TTL, bind, scope
  - `resolve_active_link()` — verify token, check expiry + bind + status
  - `revoke_link()` — disable immediately
  - `submit_proposal()` — guided/research draft submission (owner approval gate)
- **Externally Observable:**
  - Token format: URL-safe random (mint_token uses secrets.token_urlsafe)
  - Binding: first device locks to that IP (optional, for stronger auth)
  - TTL: absolute expiry (days), or never (ttl_days=0)
  - Rate limiting per IP (submit_proposal)
- **Edge Cases / Failures:**
  - Link already revoked: 404 on access
  - Bind set but different IP: 403 (access denied)
  - TTL expired: 404
- **Risk:** MEDIUM — token lifetime, binding bypass risk (client can spoof IP), rate limit effectiveness

#### `chat_share.py` — Encrypted chat (end-to-end) [MEDIUM-HIGH RISK]
- **Purpose:** Browser-encrypted channel key, server relays ciphertext only (blind relay).
- **Key Functions:**
  - `create_channel()` (line:39+) — mint chat link, store wrapped keys (owner_wrap, guest_wrap)
  - `create_pending_channel()` — draft chat (keys pending finalization in browser)
  - `finalize_channel()` — store wrapped keys, make inert → active
  - `append_message()` — allocate seq, persist if 'persist', fan relay
  - `save_to_brain()` — owner's CLIENT-DECRYPTED transcript (only plaintext entry point)
- **Externally Observable:**
  - Zero-knowledge: server never sees raw channel key, only wrapped copies
  - Message seq: monotonic, dedup-safe across reconnects
  - Persist flag: ephemeral (nothing kept) or persist (backlog)
  - OTP-required: one-time code out-of-band (stronger, but optional)
  - Auto-save after close (owner-initiated)
- **Edge Cases / Failures:**
  - Recipient files too large (>100 MB) → rejected
  - Message too large (>700 KB) → rejected
  - Pending channel leaked to recipient: 404 (safe until finalized)
  - Ephemeral channel closed: blobs deleted (guest can't recover)
- **Risk:** MEDIUM-HIGH — encryption crypto is browser-side (must be correct), server-trust assumption

#### `chat_relay.py` — In-memory pub/sub + presence for chat [LOW RISK]
- **Purpose:** Real-time SSE fan-out (stateless across restarts).
- **Key Functions:**
  - `subscribe()` — attach to channel hub, capture running loop
  - `publish()` — fan to all subscribers (except exclude)
  - `next_seq()` — allocate monotonic seq (seeds from DB max on first use)
- **Externally Observable:**
  - Cross-thread publish (call_soon_threadsafe)
  - Seq generator: seeded once per hub, increments monotonically
- **Risk:** LOW — thin transport layer, stateless

#### `labshare.py` — Lab-share lifecycle (owner side) [MEDIUM RISK]
- **Purpose:** Mint lab-share link + scope enforcement (owner-curated analyte list + date window).
- **Key Functions:**
  - `create_link()` — mint link, scope = pending (inert until activated)
  - `set_scope()` — owner ticks analytes + sets date bounds
  - `activate_link()` — approval gate, makes scope live
  - `track_recipient_query()` — audit log (what recipient looked up)
- **Externally Observable:**
  - Scope approval step (never auto-shared)
  - Identity-strip enforced by lab_share_scope (hard boundary)
  - Audit trail: recipient sessions, queries, what they saw
  - Recipient re-use: IP binding, session tracking
- **Edge Cases / Failures:**
  - Scope change: only affects NEW sessions (old recipients not re-scoped)
  - Analyte removed from allow-list: recipients lose access
- **Risk:** MEDIUM — scope enforcement, identity-strip, audit integrity

#### `labshare_ai.py` — Lab-share recipient AI (import-isolated) [MEDIUM RISK]
- **Purpose:** ISOLATED recipient AI (tool-less, reads ONLY via lab_share_scope boundary).
- **Key Functions:**
  - `answer()` — retrieve scoped results, feed to LLM, answer question
  - No direct DB access (scoped_query only entry point)
- **Externally Observable:**
  - Module imports ONLY llm, lab_share_scope (isolation invariant)
  - No notes_svc, architect, entity_index (cannot leak scope)
  - Injection guards (like research.py)
- **Risk:** MEDIUM — isolation must hold (no cross-imports allowed)

#### `guided.py` — Guided intake AI (recipient interview) [MEDIUM RISK]
- **Purpose:** Recipient-facing interview (tool-less, no brain access, writes to draft note).
- **Key Functions:**
  - `interview()` — multi-turn Socratic Q&A, streams responses
  - `finalize_submission()` — stage response as note (owner approves before save)
- **Externally Observable:**
  - Tool-less (safe: can't probe DB)
  - Draft note creation (staging_actions CREATE, not auto-saved)
  - Submission capped
  - Rate limiting per IP
- **Risk:** MEDIUM — LLM-driven Q&A quality, injection guards

---

### 9. UTILITIES & HELPERS (13 services) [LOW RISK]

#### `clock.py` — Time, timezone, live tokens
- **Purpose:** Single source of truth for owner's local time (UTC storage, local reasoning).
- **Key Functions:**
  - `app_tz()` — resolved ZoneInfo (meta 'app_tz' → env TZ → UTC)
  - `now_local()`, `today_iso()`, `now_prompt()` — local time views
  - `expand_tokens()` — @t[age:DATE] / @t[until:ISO] live-value substitution
- **Externally Observable:**
  - Fallback to UTC if zone name invalid (never crashes scheduling)
  - Time tokens are expanded when rendering notes to UI
  - Shared twin in web/src/timeTokens.ts (kept in sync)
- **Risk:** LOW — deterministic, UTC fallback safe

#### `geo.py` — Great-circle distance + bearing
- **Purpose:** Pure geo math (no DB, no network).
- **Key Functions:**
  - `distance_km()`, `bearing_deg()` — haversine formula
  - `valid_coord()` — lat -90..90, lon -180..180
- **Risk:** LOW — pure math

#### `geocode.py` — Street-address geocoding (Nominatim) [LOW RISK]
- **Purpose:** Reverse (lat/lon → address) + forward (address → lat/lon).
- **Key Functions:**
  - `reverse()` — Nominatim API, caching
  - `forward()` — address → candidates
- **Externally Observable:**
  - External API (OpenStreetMap)
  - Rate-limited
  - Caching (avoids redundant API calls)
- **Risk:** LOW — read-only, external API

#### `geotrail.py` — Geo-trail analytics (location history math)
- **Purpose:** Time-windowed location queries (nearest fix, interval stats).
- **Key Functions:**
  - `nearest_fix()` — closest timestamp in trail
  - `fixes()` — time-windowed trail
  - `label_point()` — reverse geocode
- **Risk:** LOW — read-only analytics

#### `diffing.py` — Line-level markdown diff
- **Purpose:** stdlib difflib hunks (equal/delete/insert).
- **Risk:** LOW — deterministic

#### `corruption.py` — ??? (not read; likely unused or minor)
- **Purpose:** Source-of-truth corrections (promote to entries → wiki healing)
- **Risk:** MEDIUM (read-only section below)

#### `people.py`, `places.py`, `trips.py` — Entity registries [LOW RISK]
- **Purpose:** People (name/alias), Places (geofence, name), Trips (precomputed segments).
- **Risk:** LOW — mostly read-only registries

#### `prompts.py` — Prompt template loader (prompts.yaml) [LOW RISK]
- **Purpose:** LLM prompts from config (agent system, action templates, tool descriptions).
- **Risk:** LOW — data-driven

#### `reviews.py` — Review item staging (owner approval) [LOW RISK]
- **Purpose:** Pending review cards (create, resolve, browse).
- **Risk:** LOW — straightforward staging

#### `quicktasks.py` — Quick-task items (Markdown lists) [LOW RISK]
- **Purpose:** List-item CRUD (parse/edit Markdown lists in notes).
- **Risk:** LOW — text manipulation

#### `system_status.py` — Health check + readiness [LOW RISK]
- **Purpose:** Embedding/transcription model readiness, system uptime.
- **Risk:** LOW — monitoring

#### `media_tokens.py` — Signed tokens for direct media streaming [MEDIUM RISK]
- **Purpose:** Short-lived signed tokens (img/audio/video tags can fetch attachments).
- **Key Functions:**
  - `issue_token()` — sign (attachment_id, expires_at)
  - `verify_token()` — validate signature, check expiry
- **Externally Observable:**
  - Token expiry: minutes (not hours)
  - Signature: HMAC-SHA256 over (id, expiry)
  - Token embeddable in <img src>, <audio src>, etc.
- **Risk:** MEDIUM — signature generation must be correct, token expiry timing

#### `nickname_lexicon.py` — Static nickname → formal name mapping [LOW RISK]
- **Purpose:** Bob ↔ Robert folding in entity merges.
- **Risk:** LOW — static data

#### `usage.py` — Token usage tracking [LOW RISK]
- **Purpose:** Aggregate LLM token counts (monitoring).
- **Risk:** LOW — logging

#### `push.py` — Web Push notifications [LOW RISK]
- **Purpose:** Browser push (remind, waiting guest, etc.).
- **Risk:** LOW — notification service

#### `sqlsafe.py` — SQL injection guards [LOW RISK]
- **Purpose:** Detect template-like strings (safety lint).
- **Risk:** LOW — guard rails (SQLite parameterization is the real defense)

---

### 10. ADVANCED WORKFLOWS (3 services)

#### `workflows.py` — Event-driven workflow engine [MEDIUM RISK]
- **Purpose:** Trigger-action system (on entry_created → do X).
- **Key Functions:**
  - `fire_event()` — emit event, run matching workflows
  - Workflow types: run_shell, log_entry, etc.
- **Risk:** MEDIUM — workflow execution, shell commands

#### `health_split.py` — One-time PHI privacy migration [LOW RISK]
- **Purpose:** Deterministic migration: split personal medical history out of kb/People/<Name> into kb/Health/<Name>.
- **Key Functions:**
  - `split_one()` — move section verbatim (no LLM rewrite)
- **Risk:** LOW — deterministic, migration-only, undo-able

#### `research_scope.py` — Research-link scope enforcement (server-side) [MEDIUM RISK]
- **Purpose:** APPROVED note IDs → scoped search (no out-of-scope leakage).
- **Key Functions:**
  - `scoped_search()` — search within approved_ids only
  - `scoped_query()` — SQL + approved-id filter
- **Risk:** MEDIUM — scope boundary must be airtight (hard validation)

---

### 11. LLM CORE (1 service)

#### `llm.py` — Provider-agnostic LLM abstraction [MEDIUM RISK]
- **Purpose:** Neutral interface (Message, ToolDef, ToolCall, TurnEnd), Anthropic provider today.
- **Key Functions:**
  - `stream_turn()` (line:~500+) — stream assistant response + tool calls
  - `complete()` — synchronous call (no tools)
  - Provider adapter pattern (Anthropic → neutral, extensible)
- **Externally Observable:**
  - Timeout: 120 seconds per LLM call (prevents hung agent)
  - Client caching: one client per (provider, sync/async, credentials)
  - Thinking support (Claude 3.7+ only; models don't report support, try-then-degrade)
- **Edge Cases / Failures:**
  - HTTP client FD leak (mitigation: cache clients by credentials key)
  - Hung provider (timeout kills, next call retries)
  - Model quota exceeded → transient error (agent loop catches)
- **Risk:** MEDIUM — provider integration, timeout correctness, client lifetime

---

## Major Capability Clusters & Risk Summary

### Cluster A: Ingestion & Attachment Processing [MEDIUM RISK]
- **Services:** notes, attachments, audio_transcription, image_analysis, lab_ingest, lab_parse, lab_vision
- **Logic Density:** Medium (text extraction, PDF parsing, vision API calls)
- **LLM Dependency:** Medium (image/video analysis optional but common)
- **Key Risks:**
  - PDF/image parsing: geometry-aware, format-variable
  - Vision hallucination: mitigated by faithfulness check in labs
  - Large file handling (100 MB) and truncation (silent in PDFs)
- **Hardest to Test:** Lab vision OCR (needs varied document formats, identity verification edge cases)

### Cluster B: Entity Identity & Knowledge Base [HIGH RISK]
- **Services:** note_analysis, entity_index, entity_decisions, entity_rebuild, wiki_build, rebuild_engine
- **Logic Density:** Very high (LLM synthesis, entity merging heuristics, multi-stage KB pipeline)
- **LLM Dependency:** High (note analysis, outline, article writing)
- **Key Risks:**
  - Entity merging heuristic: probabilistic name matching (false positives possible)
  - Wiki synthesis: multi-stage LLM, quality depends on models + source notes
  - Truncation recovery: auto-continue in stage 1, retry-then-fail in batch
  - Talk item dedup: normalized body matching (edge case: similar but distinct corrections)
- **Hardest to Test:**
  - Architect agent tool loop (many tools, streaming, LLM-driven)
  - Wiki synthesis pipeline (requires end-to-end source → article evaluation)
  - Entity merge chains (complex union-find, split enforcement, forced merges)

### Cluster C: Lab & Medical Data [HIGH RISK]
- **Services:** lab_parse, lab_ingest, lab_vision, lab_series, lab_share_scope
- **Logic Density:** High (coordinate-based PDF parsing, OCR identity verification)
- **LLM Dependency:** Medium (lab_vision vision API, optional)
- **Key Risks:**
  - Patient identity verification: DOB match/mismatch/unverified states
  - Faithfulness check: extracted values must appear in document (hallucination guard)
  - Parsing accuracy: geometry-dependent (column anchoring by date headers)
  - Date ambiguity: 3/7/2022 could be March 7 or July 3 (resolved by context)
  - PII stripping: lab_share_scope must null identity fields completely
- **Hardest to Test:**
  - Lab vision: varied document formats, poor photos, OCR edge cases
  - Identity verification: correct DOB matching, mismatch detection
  - Parsing recovery: transposed matrices, multi-row analytes, scanned PDFs

### Cluster D: Sharing & Recipient AI [MEDIUM-HIGH RISK]
- **Services:** chat_share, chat_relay, labshare, labshare_ai, guided, research, external_lookups
- **Logic Density:** Medium (scope enforcement, injection guards, isolated AI)
- **LLM Dependency:** Medium (recipient AIs)
- **Key Risks:**
  - Scope airtight: must never leak out-of-scope notes to recipient AI
  - Injection guards: recipient input must be sanitized (URLs, wikilinks, jailbreak phrases)
  - Identity-strip completeness: no patient name, DOB, MRN in shared labs
  - Chat encryption: zero-knowledge design (server never decrypts)
  - Recipient AI isolation: must not import notes_svc, architect, entity_index
- **Hardest to Test:**
  - Scope boundary: adversarial testing (can recipient jailbreak isolation?)
  - Injection guards: fuzzing recipient input
  - Encryption correctness: browser-side crypto, key wrapping

### Cluster E: Utilities & Infrastructure [LOW RISK]
- **Services:** clock, geo, geocode, geotrail, diffing, people, places, trips, prompts, reviews, quicktasks, system_status, media_tokens, nickname_lexicon, usage, push, sqlsafe, workflows, health_split
- **Logic Density:** Low (mostly data-driven, deterministic utilities)
- **LLM Dependency:** None
- **Key Risks:** Minimal (mostly read-only or simple mutations)

---

## Testing Difficulty Hierarchy

### 1. **Hardest (Requires Multi-Stage Validation + Domain Expertise)**
- Architect agent tool loop (architect.py)
  - Needs mock LLM or real API
  - Many interconnected tools
  - Streaming, state management
  - Quality judge for outputs
  
- Wiki synthesis (wiki_build.py + rebuild_engine.py)
  - Source → outline → article pipeline
  - LLM at multiple stages
  - Truncation recovery (continuation)
  - Lint checking + quality gates
  - Requires curated test corpus

- Lab vision + OCR (lab_vision.py)
  - Varied document formats
  - Identity verification edge cases
  - Hallucination guards
  - Coordinate-based parsing accuracy

- Entity merging (entity_index.py)
  - Name variant clustering
  - Union-find + split enforcement
  - Merge chains
  - Heuristic correctness (false positives?)

### 2. **Hard (Requires LLM Mocking or Real API)**
- Note analysis (note_analysis.py)
  - Coalesced background worker
  - Content-hash caching
  - Entity extraction accuracy
  
- Medical reference promotion (reference_promote.py + reference_refresh.py)
  - External API fetches
  - Deterministic stub building
  - Marker parsing + preservation

- Recipient AIs (research.py, labshare_ai.py, guided.py)
  - Scope enforcement validation
  - Injection guard effectiveness
  - LLM output quality

### 3. **Medium (Requires Mocking or Careful Unit Tests)**
- Lab parsing (lab_parse.py)
  - PDF geometry-aware extraction
  - Date parsing (ambiguous formats)
  - Transposed matrix reconstruction
  
- Attachment processing (attachments.py)
  - PDF text extraction accuracy
  - Chunking correctness
  - Image EXIF extraction
  
- Entity decisions (entity_decisions.py)
  - Merge chain resolution
  - Split mutual exclusion
  - Dedup logic

### 4. **Easier (Deterministic, Mockable)**
- Share links (share.py, chat_share.py, labshare.py)
  - Token generation + validation
  - Scope enforcement (SQL queries)
  - Status transitions
  
- Calendar (calendar.py)
  - Event extraction
  - Supersession detection
  - iCal RRULE expansion
  
- Search (search.py)
  - Hybrid search ranking
  - Entity expansion
  - Reciprocal rank fusion
  
- Utilities (clock.py, geo.py, diffing.py, etc.)
  - Deterministic functions
  - Pure math/string ops

---

## Conclusion

JBrain's backend is a **sophisticated, multi-stage knowledge-management system** with heavy LLM integration and deterministic foundations. The services span from simple utilities to complex agent loops, with clear architectural separation of concerns.

**Key Strengths:**
- Clear module boundaries (import-isolated recipient AIs, scoped search)
- Durable state management (append-only ledgers, crash recovery)
- Careful guards against hallucinations (faithfulness checks, content hashing)
- Thoughtful handling of external data (PDFs, images, identity verification)

**Key Challenges:**
- LLM-dependent quality (synthesis, entity clustering, image analysis)
- Complex multi-stage pipelines (wiki build, lab vision, entity rebuild)
- Truncation recovery (auto-continue in gather, retry-then-fail in batch)
- Probabilistic heuristics (entity merging, date parsing ambiguity)

**Testing Strategy Recommendation:**
- **Unit tests** for deterministic modules (calendar, search, geo, diffing)
- **Integration tests** with mocked LLM for architect, synthesis, note analysis
- **E2E tests** for critical workflows (lab ingestion, share link, entity rebuild)
- **Fuzzing** for injection guards, date parsing, entity merge edge cases
- **Manual validation** for lab vision, wiki quality (requires domain expertise)

