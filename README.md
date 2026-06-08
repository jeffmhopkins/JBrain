# JBrain

Your self-hosted **conversational wiki** and thinking partner. Talk to it; a
Socratic AI "Knowledge Architect" asks questions, then proposes well-linked
notes for you to confirm. Everything lives in a single SQLite database on
**your** Linux VM — queryable, versioned, and browsable as a wiki, with
backlinks, semantic search, a knowledge graph, a location trail, and a
continually-synthesised knowledge base. Installable as a PWA on your phone and
desktop, with optional native Android phone + Wear OS capture clients.

It's a self-hosted, owned-end-to-end evolution of the "Gemini + Google Drive"
conversational-wiki idea — but with a real database, a proper wiki UI, hybrid
search, a graph, and a programmable automation layer.

> **At a glance:** FastAPI + SQLite backend, React + Vite PWA front end, an
> LLM-provider-agnostic agent (Anthropic Claude *or* xAI Grok), **local**
> embeddings, transcription, and OCR (no extra API keys), and a declarative
> trigger→action workflow engine. All shipped as Docker Compose behind Caddy
> (automatic HTTPS). Current app/schema: `v0.1.162` / schema `v56`.

## Features

### Capture & conversation

- **Compose-centric UI** — the home screen is a single rounded compose box: type
  and Send, with a **3-mode segmented control** (Entry · Research · Full Brain) on
  its own row, **attach** + **voice dictation** below, and a **lightning bolt**
  (top-right) that opens the **Advanced** launcher. A fresh launch defaults to
  **Research**; your last mode is remembered within a session.
  - **Entry** — stored directly (no LLM), running the `entry_created` hooks
    (auto-tag, etc.). A **sub-selector** picks where it files: **Generic** (the
    dated tree), **Medical** (`notes/medical/<dest>/` + lab-PDF staging for review),
    or **Financial** (`notes/financial/<dest>/`).
  - **Research** — a **read-only** Q&A over your brain (hybrid keyword/semantic
    search + a SELECT-only `query_sql`); it never modifies anything. A **Deep**
    toggle raises the token budget for multi-step questions without loosening its
    strict, facts-only posture.
  - **Full Brain** — the Socratic architect with full tool access: talks a topic
    out, then proposes a note to a **staging area** you confirm with **Apply**. It
    also handles quick additive ops ("add milk to the shopping list", "log a 5k
    run") that apply instantly with one-tap **Undo**, and can consult your curated
    reference library. No destructive auto-apply — deletes/edits go through staging.
- **Grounded agent** — the architect never asserts a stored fact without a tool
  call *this turn*, only links exact note titles a tool returned this session, and
  separates thinking from asserting. Two providers are supported behind one seam:
  **Anthropic Claude** (default `claude-sonnet-4-6`, with opt-in extended
  thinking) and **xAI Grok** — selectable per install. Cheaper sub-tasks
  (tagging, summaries, filing, vision) can route to a cheaper model.
- **Editable prompts** — every prompt (the three architect modes, ~50 tool
  descriptions, and the workflow AI actions) lives in `prompts.yaml`, hot-reloaded
  on change and editable from the app (**Advanced → Prompts**). In-app edits are
  stored in the DB (so they survive updates); "Reset to default" returns to the
  shipped `prompts.yaml`.

### Notes, wiki & knowledge base

- **Wiki** — markdown notes with `[[wiki-links]]` (and `[[Target|display]]`
  aliases), automatic **backlinks**, and full **revision history**: every edit is
  versioned and attributed (you vs. the AI vs. a restore vs. a workflow), with
  **line diffs** and one-click **restore** (which snapshots first, so history is
  never lost). Edit any note's markdown directly in the PWA.
- **Knowledge-base synthesis** — scheduled workflows fold the durable knowledge
  from your raw entries into a continually-updated **Knowledge Base** layer
  (`kb/…`) that cites back to the source entries. It runs as layered passes — an
  incremental **nightly update**, a nightly **maintenance** pass that resolves
  open questions/conflicts, and a manual full **rebuild** — all versioned and
  auto-applied, each posting a Review card. Browse it via **Wiki → Knowledge base**.
- **Article talk pages** — KB articles carry a Wikipedia-style **Talk** panel
  (decisions, conflicts, questions, directives, corrections) that a maintenance
  workflow reads and acts on, so you can steer synthesis in plain language.
- **Entities** — a canonical index of the people, organisations, places, things,
  conditions, meds, procedures, events, and concepts mentioned across your notes,
  with mentions, aliases, and **merge / keep-separate** decisions surfaced for
  review.
- **Knowledge graph** — an interactive force-directed map of notes and their
  links, filterable by kind and hop-depth, with focus + tap-to-open.
- **Day-log summaries** — log to a "Daily Log" through the day; the first entry of
  a new day auto-summarises the previous day into a "Daily Summaries" note and
  posts a review card (a built-in workflow).

### Attachments & media

- **Attachments** — attach **any number of files, up to 100 MB each**, to a note
  (stored in the DB so backups stay complete) — both on a note's page and right
  from the compose box. **Audio & video play inline.** Searchable text is extracted
  automatically: text/code files are decoded, **PDFs** are text-extracted (pypdf),
  and **image EXIF/metadata** is pulled — all chunked and indexed for keyword +
  semantic search.
- **AI enrichment** (with Auto-analyze on, below): **images** get a vision summary,
  and **audio & video** are **transcribed locally** (faster-whisper, no API key —
  video via its decoded audio track), with **video frames sampled** on a
  configurable cadence and described by the vision model so on-screen content is
  captured too. The per-note **AI analysis** sidecar folds in this attachment
  content (PDF/document text, transcripts, image summaries) so its gist/facts/
  entities reflect what's in your files.
- **Auto-analyze new notes & their attachments** — a **master toggle** (**System →
  Note analysis**, off by default, backed by the `analyze-new-note` workflow). On:
  a note's analysis is computed the moment you add it and its attachments
  auto-enrich. Off: nothing auto-processes (you can still analyze/transcribe any
  attachment by hand from its panel; the note's own analysis still runs nightly).
  Vision & note analysis need an LLM key; transcription and OCR are local.

### Search & data

- **Search** — hybrid **keyword (FTS5/BM25)** + **semantic** (local `fastembed`
  embeddings, `BAAI/bge-small-en-v1.5`, no extra API key) fused by reciprocal
  rank, plus dedicated **keyword**, **semantic**, and **entities** modes. Notes,
  attachment chunks, and entities are all indexed.
- **SQL access** — a built-in **read-only** SQL console (`SELECT`/`WITH` only,
  enforced at the engine, a `query_only` pragma, *and* a SQLite authorizer that
  denies sensitive tables/columns), plus the full `sqlite3` CLI for power users.
- **Backup & restore** — the whole brain is one SQLite file; export/import a
  consistent snapshot from the app or the CLI (see below).

### Location, calendar & health

- **Location & time** — entries always record *when* they were made and, with the
  opt-in 📍 toggle, *where* (a one-shot best-effort fix at send time). A **Map**
  view shows your **location trail + heatmap** with time ranges and "notes here"
  radius search; the backend does dwell detection, **trip** segmentation, and
  **place** attribution. The native Android client can additionally record a
  continuous background trail.
- **Calendar** — appointments and recurring events with reminders; a per-minute
  **calendar-alarms** workflow fires reminders ahead of each appointment, and a
  workflow can derive events from your dated notes.
- **Medical & labs** — Medical-mode capture files clinical notes and **stages lab
  PDFs** for review; a **Labs** view charts result trends over time. Health and
  finance KB articles are treated as **private** (see Shares firewall below).

### Sharing & collaboration

- **Public share links** — mint **read** or **edit** links to a note (edits arrive
  as **proposals** you review, never direct writes), with optional **device-bind**
  and **TTL**. Links to private **health/finance** KB articles are firewalled:
  always device-bound and capped to a short TTL.
- **Guided intake** — a link that runs an AI **interview**, drafts an intake
  document from the conversation, and submits it for your review.
- **Research links** — a read-only link that lets someone ask questions over a
  curated, approved set of notes.
- **Lab shares** — a recipient-facing AI with a deliberately **scoped, read-only**
  toolset (no note IDs, no owner SQL) for sharing lab context safely.
- **Encrypted chat (share-link)** — send someone a link and chat in real time,
  **end-to-end encrypted**: an AES-256-GCM channel key is generated in your browser
  and rides the link's `#fragment` (never sent to the server), so JBrain relays
  only opaque ciphertext — messages **and attachments**. Optionally require a
  **one-time code** out-of-band, and confirm an **emoji safety check** (SAS
  fingerprint) to rule out a man-in-the-middle. Strictly **1:1** (the link locks to
  the first browser that joins). When a recipient opens the link and you're away,
  you get a **push notification** to join in one tap. On close, the conversation is
  **saved to your brain** as a normal note (decrypted on your device) —
  searchable, graph-linked, and analyzable. Per-chat choice of **persisted**
  (encrypted backlog kept) or **ephemeral** (relay only). Manage them under
  **Shares**.

### Automation & review

- **Workflows** — trigger→action automations defined as repo YAML (`workflows/`,
  ~21 shipped), ingested into the DB on boot and editable in the PWA. Triggers:
  app **events** (e.g. `entry_created`), fixed **intervals**, **cron** (`"0 7 * * *"`,
  in the server timezone), or **geofences**. Each workflow points at an **action**
  recipe (`actions/`, declarative step lists) and an editable prompt. Runs are
  logged; writes are versioned and attributed `source='workflow'`. Built-ins cover
  nightly note analysis, day-log/daily consolidation, KB update/maintenance,
  calendar alarms, link-label audits, place discovery, and more. Turn them on/off,
  edit, **run now**, or **re-sync from the repo** anytime (a workflow you edited
  shows *Reset to repo*).
- **Review inbox** — workflows post **review items** (title, message, link, dismiss)
  surfaced in a PWA **Review** tab with a count badge (and optional **web-push**
  notifications) — daily-review messages, entity-merge suggestions, share
  proposals, and the like. A 24-hour archive of dismissed items is also kept.

### Platform

- **PWA** — installable, responsive (first-class phone *and* desktop layouts),
  with **offline reading** of notes you've already viewed (Workbox NetworkFirst
  caching), an hourly update poll, and a server↔app **version-mismatch banner**.
- **Native Android capture** (optional) — a **phone** app (home-screen photo &
  dictation capture widgets, background location trail, watch relay, setup-code
  onboarding) and a **Wear OS watch** app (one-tap dictation tile that relays to
  the phone, with an offline queue). The phone holds the access key; the watch
  holds none. Built and published as APK artifacts by CI (`android-apk.yml`).
- **The Advanced launcher** — the lightning bolt opens a calm grid of tools in
  three sections:
  - **Knowledge** — Wiki, Lists, Calendar, Search, Graph, Entities, Map, Users
    (trail attribution), Medical, Labs.
  - **Authoring** — Prompts, Actions (step recipes), Triggers (when actions run).
  - **System** — Shares, Data (SQL + backup), System (version · settings).

## Architecture

JBrain is one small Docker stack on your own Linux VM. There is no cloud tier and
no third-party datastore: every note, attachment, and revision lives in **one
SQLite file** on a volume *you* own. Heavy ML — embeddings, transcription, OCR —
runs **locally**; only the LLM calls leave the box, through a single swappable
seam. The same backend serves the owner's PWA, the native Android clients, and
public share links, all over `Authorization: Bearer <access-key>`.

### The big picture

```text
  Phone / desktop PWA          Native Android (phone + Wear OS)
        │                             │
        └──────────── HTTPS ──────────┘    Authorization: Bearer <key>
                      │
                      ▼
  ┌─ Caddy ────────────────────────────────────────────────
  │  TLS (auto Let's Encrypt) · reverse proxy · ports 80/443
  │  unbuffered SSE for /api/chat/* · security headers / CSP
  │  access-key-gated /deploy-status console (survives restarts)
  └──────────────────────────────┬─────────────────────────
                                 │  reverse_proxy → api:8000
                                 ▼
  ┌─ api · FastAPI (Python 3.12) + built React 18 PWA ──────
  │  ~29 routers  →  ~60 services
  │   • LLM seam      → Anthropic Claude (default) | xAI Grok
  │   • local ML      → fastembed · faster-whisper · tesseract
  │   • hybrid search → FTS5 / BM25  +  sqlite-vec vectors
  └──────────────────────────────┬─────────────────────────
                                 │  one SQLite connection per request
                                 ▼
  ┌─ brain.db · one SQLite file (~64 tables, schema v56) ───
  │  notes · versions · attachments(BLOB) · search index ·
  │  conversations · workflows · entities · shares · system
  └─────────────────────────────────────────────────────────
  volume: brain-data    (+ caddy-data, caddy-config, model-cache)

  updater (optional sidecar, profile: autoupdate) rebuilds the api image
  on a PWA-triggered update; mounts the Docker socket — off by default.
```

A single FastAPI process *is* the application: it serves the JSON API **and** the
pre-built React PWA as static files. To keep the event loop responsive, every
blocking unit — embedding inference, LLM calls, SQLite writes — is offloaded to a
worker thread (`asyncio.to_thread`), and each request gets its own serialized
connection. The frontend (`web/`) is React 18 + Vite 5 + TypeScript with React
Router 6, `vite-plugin-pwa`/Workbox, `react-force-graph-2d` (graph), and
`leaflet` + `leaflet.heat` (map). Cheaper sub-tasks (tagging, filing, vision) can
route to a cheaper model through the same LLM seam; extended thinking is
Anthropic-only.

### Data flow

**Capture (Entry mode) — no LLM on the hot path.** Send returns immediately;
enrichment happens after the commit so a slow hook never holds the write lock.

```text
PWA compose · Entry        POST /api/notes/entry {text, dest?, lat?, lon?}
   │
   ▼
notes_svc.upsert_note()  ── one transaction ──
   ├─ INSERT notes        (title notes/YYYY/MM/DD/NN, or .../<dest>/NN)
   ├─ INSERT note_versions (source='user')
   ├─ reconcile [[wiki-links]] → links
   ├─ index  → notes_fts (FTS5)
   └─ embed  → vec_notes / vec_note_chunks (fastembed)
   │  commit, return to the client
   ▼
after commit (best-effort): fire 'entry_created' workflows (auto-tag),
and — only if "auto-analyze new notes" is on — run the note_analysis pass
```

**Chat turn (Full Brain / Research) — a streamed, grounded agent loop.** Nothing
touches the wiki until you tap **Apply**.

```text
PWA ── POST /api/chat/conversations/{id}/message   (mode: assisted|research)
       response streams back as SSE; Caddy keeps /api/chat/* unbuffered
   │
   ▼
architect.run()  — async generator, bounded by max-iterations + token budget
   repeat:
     llm.stream_turn()   → token deltas stream straight to the client;
        │                  the model may request tool calls (~50 tools)
        ▼
     await asyncio.to_thread(run_tool)   ← search, query_sql (SELECT-only),
        │                                  kb sub-calls, SQLite writes
        ▼
     propose_actions → INSERT staging_actions (status='pending')
                       emits a 'staging' event → the Staging area card
   reply + tool-step history persisted; 'done'

   user taps Apply → POST /api/staging/{id}/apply
        claims the row (pending → applied) and writes the note,
        versioned source='architect'; destructive ops keep a one-tap Undo
```

Research mode gets a strictly read-only tool set; an unrecognized mode fails
**closed** to read-only.

**Attachment enrichment.** Bytes are stored as a BLOB; text is always extracted,
and (with auto-analyze) local models add a richer sidecar.

```text
POST /api/notes/{slug}/attachments   (multipart, ≤ 100 MB → BLOB in DB)
   │
   ▼
extract text   PDF → pypdf (≤100 pg / 200 KB) · image → PIL EXIF/GPS ·
   │           text/code → decode · scans → tesseract OCR
   ├─ chunk (1200 chars, 200 overlap, header-aware)
   └─ index → attachments_fts (FTS5) + vec_chunks (embeddings)
   │
   ▼  if "auto-analyze" is on (else: manual Analyze / Transcribe button)
   image → vision summary · audio → faster-whisper (local) ·
   video → PyAV audio → whisper  +  frames sampled → vision model
   │   written to the attachment's analysis_md sidecar
   │   status pending → done | error   (30-min stale watchdog)
   ▼
   folds into the note's note_analysis  (gist · facts · tags)
```

**Search.** Both halves over-fetch, then fuse; attachment hits credit their note.

```text
GET /api/search?q=…&mode=hybrid | keyword | semantic | entities
   │
   ├─ keyword   → notes_fts + attachments_fts          (FTS5 / BM25)
   ├─ semantic  → sqlite-vec nearest-neighbour over note/chunk/entity vecs
   └─ entities  → canonical index matched by name + alias (+ vector)
   │
   ▼  reciprocal-rank fusion blends the sources → dedup best-per-note
   ranked results (semantic degrades to keyword while the model warms up)
```

### Storage model

One SQLite file (`/data/brain.db`, ~64 tables, schema `v56`); a 50+ step migration
runner upgrades it on boot, so an old backup imports cleanly. **Attachments are
stored as BLOBs**, so that single `.db` is a complete, self-contained backup —
notes, history, files, and config together.

| Group | Tables (representative) |
|-------|-------------------------|
| Content | `notes`, `note_versions`, `note_chunks`, `note_analysis`, `links`, `attachments` (+BLOB) |
| Search | `notes_fts`, `attachments_fts` (FTS5); `vec_notes`, `vec_chunks`, `vec_note_chunks`, `vec_entities` (sqlite-vec) |
| Conversation | `conversations`, `messages`, `message_steps`, `staging_actions` |
| Automation | `workflows`, `action_defs`, `workflow_runs`, `review_items` |
| Entities | `entities`, `entity_mentions`, `entity_aliases`, `entity_decisions` |
| Sharing | `share_links`, `*_specs` / `*_sessions`, `chat_channels` / `chat_messages` / `chat_files` |
| Location / health / calendar | `locations`, `trips`, `places`; `lab_results`, `vitals`, `medications`; `calendar_events` |
| System | `meta`, `llm_usage`, `push_subscriptions` |

The FTS5 indexes are defined in `schema.sql`; the `vec_*` virtual tables are built
in `db.py` at startup. (The full inventory is one `SELECT` away in the SQL console.)

### Security & privacy posture

- **One credential.** No usernames/passwords — a single high-entropy **access
  key** over Caddy's TLS. The server stores only its **SHA-256 hash** and compares
  in constant time.
- **Read-only SQL is read-only.** The in-app console and the agent's `query_sql`
  accept only `SELECT`/`WITH`, enforced at **three layers**: a statement check,
  SQLite's `query_only` pragma, and an engine **authorizer** that denies sensitive
  tables/columns (access-key hash, share secrets, location keys…).
- **Private domains are firewalled.** `kb/Health/*` and `kb/Finance/*` share links
  are always device-bound and short-TTL — a boot-time assertion refuses to start if
  that clamp is ever bypassed.
- **Share-link chat is end-to-end encrypted.** The **AES-256-GCM** channel key is
  generated in the browser and rides the link's URL `#fragment` (PBKDF2-200k,
  optional out-of-band OTP), so JBrain relays only opaque ciphertext. The share
  token itself is an unguessable **capability secret**, not ciphertext. (Your own
  notes are, of course, plaintext at rest in your DB — that's what makes them
  searchable and analyzable.)
- **ML stays local.** Embeddings, transcription, and OCR need no external service;
  only your chosen LLM provider ever sees prompt content.

## Authentication

There are no usernames or passwords. A single high-entropy **access key** (the
"cert") is the only credential:

- It's generated at install (or by the server on first run, printed to the logs
  and saved to `/data/access-key.txt`).
- You **paste it once** into the PWA on first run; it's stored on that device and
  sent as `Authorization: Bearer <key>` over HTTPS with every request. The phone
  app uses the same key; the watch holds no key — it relays dictations to the
  phone, which forwards them.
- The server stores only a SHA-256 hash and compares in constant time.
- **Rotate** the key by editing `JBRAIN_ACCESS_KEY` in `.env` and restarting; old
  devices simply re-paste the new key.

Transport is already encrypted by Caddy's TLS, so the key authenticates each call
rather than adding a second encryption layer. (Share-link end-to-end encryption is
separate and additive — see *Encrypted chat* above.)

## Quick start (Linux VM)

Prerequisites: **Docker Engine + Compose v2**, a **domain** whose A record points
at the VM (set this up *before* installing so Caddy can issue the cert), ports
**80/443** open, and **≥ 2 GB RAM** (the local embedding/whisper models load into
memory).

```bash
git clone <your-fork-url> JBrain && cd JBrain
./install.sh
```

The installer asks for your domain + ACME email, brain name, **LLM provider**
(Anthropic Claude or xAI Grok) + model + API key, timezone, and whether to enable
the auto-update sidecar. It generates a high-entropy **access key** (printed at the
end — save it), writes `.env` + `Caddyfile`, and offers to build & start
everything. When it's up:

1. Open `https://<your-domain>` and **paste your access key** to connect.
2. Use the browser's **Install app** / **Add to Home Screen** to install the PWA.
3. Start in the compose box. **Research** answers from your brain read-only; switch
   to **Full Brain** to have the architect propose notes — tap **Apply** on the
   **Staging area** to write them.

Manual control:

```bash
docker compose up -d --build     # build & start (api + caddy)
docker compose logs -f           # follow logs
docker compose down              # stop
```

> First boot downloads the local embedding model (a few hundred MB) and the local
> speech-to-text model (`AUDIO_MODEL`, ~140 MB for `base`) from Hugging Face and
> warms them in the background, so the server is usable immediately and only the
> very first semantic search / transcription may wait. Needs runtime network
> egress on first boot and **≥ 2 GB RAM** (set `AUDIO_MODEL=tiny` and/or raise
> `MEM_LIMIT` on a tight box). Both run without any external API key.
>
> Running a **fork**? Set `JBRAIN_REPO=owner/name` in `.env` so the in-app update
> checker points at your repo. The auto-update sidecar (`COMPOSE_PROFILES=autoupdate`)
> mounts the Docker socket (host-root-equivalent) — leave it **off** unless you
> want PWA-triggered updates, and use `./update.sh` for manual updates instead.

## Local LLM (Ollama) — optional, hybrid

Routine, high-volume jobs (tags, day summaries, note filing, date/place extraction —
the `models.cheap` tier) can run on a **local** OpenAI-compatible model via
[Ollama](https://ollama.com), with **no API key and nothing leaving the box**, while
the interactive agent, KB synthesis, and vision stay on the cloud API. Embeddings and
speech-to-text are already local. A local-tier failure falls back to the cloud default
(`LLM_LOCAL_FALLBACK`), so an outage degrades rather than breaks.

Two ways to run it:

- **Mode A — turnkey (recommended).** Add `localllm` to `COMPOSE_PROFILES`; JBrain runs
  Ollama in a container and pulls `LLM_LOCAL_MODEL` on first boot (background, resumable).
  Use the default URLs (`http://ollama:11434…`).
- **Mode B — bring your own.** Install Ollama on the host
  (`curl -fsSL https://ollama.com/install.sh | sh`) and set the URLs to
  `http://host.docker.internal:11434(/v1)`.

```dotenv
# .env
COMPOSE_PROFILES=localllm
LLM_LOCAL_ENABLE=true
LLM_LOCAL_BASE_URL=http://ollama:11434/v1
LLM_LOCAL_ADMIN_URL=http://ollama:11434
LLM_LOCAL_MODEL=qwen2.5:7b
LLM_TIMEOUT_SECONDS=600
```

Then assign a tier to the model in **System → Model** (or set `models.cheap: "qwen2.5:7b"`
in `prompts.yaml`). Manage installed models — pull/remove with live progress — in
**System → Local models**, which is hardware-aware and warns when a model won't fit.

**Hardware:** local inference is CPU/RAM-bandwidth bound. On a 32 GB box, use a **7–8 B**
quantized model (≈ 6 GB resident), allow up to ~13 B, and **avoid 70 B**. Generation runs
at tens of tokens/sec on a GPU box but only a few tok/s on a DDR4 CPU — fine for the
background `cheap` tier, which is exactly what to offload. Health (`down`/`pulling`/
`warming`/`ready`) shows on the status dot.

Both `install.sh` and **System → Local models** also offer **larger models** —
`qwen2.5:14b` (~12 GB), `qwen2.5:32b` (~26 GB), `llama3.3:70b` (~56 GB), and the
`gpt-oss:120b` MoE (~85 GB) — for a **high-memory machine** (e.g. a 128 GB unified-memory
Strix Halo mini PC). The RAM each needs is shown next to it, and the UI **disables any
model that won't fit** the detected memory, so the big ones are selectable only where the
box can actually run them. With enough memory you can route the chat agent locally too —
not just the `cheap` tier.

## Hosting the PWA on GitHub Pages (optional)

The PWA can run from GitHub Pages instead of being served by your server — useful
to install/update the app independently of the VM.

- GitHub Pages serves *built static files*, so `.github/workflows/pages.yml`
  builds `web/` and publishes it. Enable it in **Settings → Pages → Source: GitHub
  Actions**; it deploys on pushes to `main` that touch `web/`.
- Because the app is then a different origin from your server:
  - **First run asks for your server address** (your VM's `https://…`) plus the
    access key; both are stored on-device and used for every request.
  - The server allows cross-origin calls via **CORS** (`JBRAIN_CORS_ORIGINS`,
    default `*`; safe since auth is a bearer token, not cookies).
  - The app checks the **server version** against its own and shows a banner if
    they differ.
- Served by your server instead (the default)? Leave the server address blank —
  everything is same-origin and works as before.

## SQL access

- **In-app**: **Advanced → Data** runs read-only `SELECT` / `WITH` queries.
- **Full CLI**:
  ```bash
  docker compose exec api sh -c "apt-get update && apt-get install -y sqlite3" # if needed
  docker compose exec api sqlite3 /data/brain.db
  ```

## Updating

JBrain shows an **Update** banner in the PWA when a newer version exists. By
default it **tracks `main` by commit** (the image bakes its git commit in via the
`GIT_SHA` build arg, compared against the latest commit on `main`) — so you don't
need to cut releases; just push to `main`. If you prefer versioned releases, a
published GitHub Release/tag newer than `APP_VERSION` takes precedence. Updating is
**non-destructive**: the database, model cache, and Caddy certs are on Docker named
volumes and your `.env` is untouched — only code is replaced, and schema changes
are applied by the migration runner on boot.

**Fully automatic (recommended):** enable the updater sidecar — answer "yes" in
`install.sh`, or set `COMPOSE_PROFILES=autoupdate` in `.env` and `docker compose up
-d`. Then tapping **Update** in the PWA is end-to-end: the server writes an update
request, the `updater` container pulls the latest release/commit, rebuilds the
`api` image, health-checks it, and restarts (Caddy and the updater keep running;
the updater even self-re-execs if its own script changed). The updater needs the
Docker socket and the project directory — that's the trade-off for hands-off
updates.

**Manual alternatives** (if you don't enable the updater):
- Run `./update.sh` (optionally `./update.sh v0.2.0`) on the host — it fetches,
  fast-forwards, rebuilds, re-renders + **validates** the Caddyfile, and polls
  `/api/health` before declaring success.
- Or set `JBRAIN_UPDATE_CMD` so the **Update** button runs it in the api
  container, or wire your own host watcher to the `data/update-requested.json`
  marker the server writes.

> Keep `COMPOSE_PROJECT_NAME` stable (set by `install.sh`) so the updater targets
> the same stack you launched.

Workflows can be re-synced from the repo anytime (**Triggers → *Sync from repo***);
a workflow you edited shows *Reset to repo* to track the shipped definition again.

## Backup & restore

The whole brain — notes, attachments, history, workflows — is one SQLite file.

**From the app (easiest):** **Advanced → Data → Export database** downloads a
consistent snapshot (`.db`); **Import database…** replaces everything from a backup
(it's upgraded to the current schema on import, and your configured access key
stays valid). Available at `GET /api/system/backup` and `POST /api/system/restore`.

**From the CLI:**

```bash
# Backup
docker compose exec api sqlite3 /data/brain.db ".backup '/data/brain-backup.db'"
docker compose cp api:/data/brain-backup.db ./brain-backup.db

# Restore into a fresh deploy
docker compose cp ./brain-backup.db api:/data/brain.db
docker compose restart api
```

## Development

```bash
# Backend (FastAPI, hot reload; proxied as /api in dev)
cd server && pip install -r requirements.txt && uvicorn app.main:app --reload
# Frontend (Vite dev server, proxies /api to :8000)
cd web && npm install && npm run dev
```

Repo layout:

- `server/` — FastAPI + SQLite backend (routers, services). Tests in `server/tests/`.
- `web/` — React + Vite PWA. Colocated `*.test.tsx` next to source.
- `e2e/` — Playwright system tests (real PWA + API, LLM faked at the boundary).
- `android/` — Kotlin phone (`app/`) + Wear OS (`wear/`) capture clients.
- `workflows/`, `actions/` — declarative automations (validated by the `flows` tier).
- `prompts.yaml` — the single source for agent modes, tool descriptions, and
  workflow action prompts (hot-reloaded; in-app edits persist in the DB).
- `docs/` — testing design & coverage history (`docs/testing-plan/`,
  `docs/coverage-audit/`), with historical design plans for shipped features under
  `docs/archive/` (see `docs/README.md` for the map).

## Testing

JBrain ships a unified test runner and an honor-system **Definition of Done**.
CI reports results per domain but does **not** block merges — the policy below is
why the suite stays trustworthy.

### One command — `./jt`

```
./jt            # the gate: backend (minus concurrency) + frontend
./jt back [..]  # pytest (passes args, e.g. ./jt back -k notes)
./jt front      # vitest run
./jt unit       # the fast `unit` tier across both domains
./jt cov        # both domains with coverage + per-domain floors
./jt e2e        # build the PWA + run Playwright (LLM faked)
./jt android    # Android JVM unit tests (Robolectric, no emulator)
./jt ci         # run every tier and print a PASS/FAIL summary
./jt install …  # install per-domain test deps (back|front|e2e|android)
```

Native commands still work: `cd server && pytest`, `cd web && npm test`.

### Test taxonomy (one vocabulary)

- **unit** — isolated, no DB/network/server. Backend marker `@pytest.mark.unit`;
  frontend pure-logic `*.test.ts`.
- **integration** — real intra-domain wiring, externals mocked (LLM via the `llm`
  module seam, embeddings stubbed; frontend via MSW + `renderWithProviders`). The
  default tier.
- **concurrency** *(backend only)* — real threads / on-disk WAL contention; runs
  serially. Skipped by the local `./jt` gate for speed; CI runs it.
- **flows** — validates every `workflows/*.yaml` + `actions/*.yaml` (schema, cron,
  primitive registry).
- **system / e2e** — Playwright in `e2e/`, real PWA + API + SQLite, with the LLM
  faked at the boundary (`e2e/fake_llm.py` — never a real key). Journeys cover
  first-run auth, Entry capture + search, Full-Brain propose→apply, workflow
  run-now → Review, research, and share flows.

### Definition of Done (apply to EVERY change)

A change is not "done" until:

1. **Tests exist for the change.** New feature → new tests in the right tier. Bug
   fix → a test that fails before the fix and passes after. Put the test where its
   peers live and follow neighbouring patterns (canonical fixtures/seams
   server-side; `renderWithProviders` + MSW client-side).
2. **`./jt` is green** for the domain(s) you touched (run `./jt e2e` too if you
   changed a user-facing flow or the API contract behind one).
3. **Coverage does not regress.** Floors are `fail_under` in `server/pyproject.toml`
   and `thresholds` in `web/vitest.config.ts`. Never lower a floor to make CI pass;
   when real coverage clears the floor comfortably, **ratchet the floor up** in the
   same change.
4. **Production code is the only thing changed for behaviour** — don't weaken a
   test to make it pass; fix the code or the expectation honestly.
5. **No real network/LLM/secrets in tests.** Mock at the module seam; the LLM is
   faked at the boundary in e2e, never a real key. Embeddings are always stubbed.

### CI (informational, per-domain)

`.github/workflows/test.yml` runs four jobs on every PR — **back**, **front**,
**e2e**, **android** — each enforcing its own coverage floor and reporting
pass/fail. A red check means the Definition of Done isn't met yet. (Other
workflows: `pages.yml` publishes the PWA to GitHub Pages; `android-apk.yml` builds
signed phone + watch APKs.)

## Configuration

All config is environment-driven (`.env`, see `.env.example`). Media settings are
also editable in-app under **System → Media & transcription** (stored in the DB and
read at runtime, so changes apply with no restart).

| Variable | Default | Purpose |
|----------|---------|---------|
| `JBRAIN_DOMAIN` | `localhost` | Public domain (A record + Caddy cert) |
| `ACME_EMAIL` | — | Let's Encrypt contact email |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `xai` |
| `LLM_API_KEY` | — | Key for the chosen provider (legacy `ANTHROPIC_API_KEY` aliases this) |
| `LLM_MODEL` | `claude-sonnet-4-6` | Model id (legacy `ANTHROPIC_MODEL` aliases this) |
| `XAI_API_KEY` / `XAI_BASE_URL` | — / `https://api.x.ai/v1` | xAI Grok credentials/endpoint |
| `LLM_LOCAL_ENABLE` | `false` | Enable local (Ollama) model routing — see **Local LLM** below |
| `LLM_LOCAL_BASE_URL` | `http://ollama:11434/v1` | Local OpenAI-compatible endpoint (Mode A in-compose; Mode B `host.docker.internal`) |
| `LLM_LOCAL_MODEL` | — | Ollama model tag to pull/serve, e.g. `qwen2.5:7b` |
| `LLM_LOCAL_FALLBACK` | `true` | On local failure, retry on the cloud default (degrade, not break) |
| `LLM_TIMEOUT_SECONDS` | `120` | Per-request LLM timeout — raise (~600) for slow CPU inference |
| `OLLAMA_MEM_LIMIT` | `8g` | RSS cap for the in-compose Ollama container (Mode A) |
| `BRAIN_NAME` | `My Brain` | Display name |
| `JBRAIN_ACCESS_KEY` | *(generated)* | The cert; if blank the server generates one on first boot |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Local embedding model (384-dim) |
| `AUDIO_MODEL` | `base` | Whisper size: `tiny`…`large-v3` |
| `AUDIO_COMPUTE_TYPE` | `int8` | Whisper compute type (RAM/speed trade-off) |
| `VIDEO_FRAME_INTERVAL` | `30s` | Frame sampling cadence (time `30s` or percent `25%`) |
| `VIDEO_FRAME_MAX` | `8` | Hard frame cap (`0` = video vision off, transcript only) |
| `DB_PATH` | `/data/brain.db` | SQLite path inside the container |
| `TZ` | `UTC` | Server timezone — drives cron jobs and date-bucketing |
| `JBRAIN_CORS_ORIGINS` | `*` | Allowed CORS origins (bearer auth, no cookies) |
| `MEM_LIMIT` | `1536m` | API container memory limit — raise for larger local models |
| `VAPID_SUBJECT` | `mailto:…` | Web-Push contact (keypair auto-generated on first boot) |
| `GEOCODER_URL` | Nominatim | Reverse/forward geocoder (blank disables) |
| `JBRAIN_REPO` | `jeffmhopkins/JBrain` | Repo the in-app update checker points at (set for forks) |
| `JBRAIN_UPDATE_CMD` | — | Optional command the **Update** button runs in-container |
| `COMPOSE_PROFILES` | — | `autoupdate` enables the updater sidecar |
| `COMPOSE_PROJECT_NAME` | `jbrain` | Compose project name (keep stable for the updater) |

## Roadmap

Multi-user accounts · full offline sync (write-behind) · richer finance domain ·
broader e2e/system + Android test coverage · standalone (phone-independent) Wear OS
capture · tag-management UI.
