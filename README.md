# JBrain

Your self-hosted **conversational wiki** and thinking partner. Talk to it; a
Socratic AI "Knowledge Architect" (powered by Claude) asks questions, then
proposes well-linked notes for you to confirm. Everything lives in a SQL
database on **your** Linux VM — queryable, versioned, and browsable as a wiki,
with backlinks, semantic search, and a knowledge graph. Installable as a PWA on
your phone and desktop.

It's a self-hosted, owned-end-to-end evolution of the "Gemini + Google Drive"
conversational wiki idea — but with a real database, a proper wiki UI, search,
and a graph.

## Features

- **Conversational capture** — a curious, Socratic Claude agent that draws ideas
  out of you one concept at a time.
- **Compose-centric UI** — the home screen is a single rounded compose box: type
  and Send, with a **mode chip** (Entry / Assisted / Research) on the left and
  **attach** + **voice dictation** on the right. A **lightning bolt** (top-right)
  opens **Advanced** — a grouped nav (Browse · Automate · Data · Review). The
  three capture modes:
  - **Entry** — type a note; it's stored directly (no LLM) and runs the
    `entry_created` hooks (auto-tag, etc.).
  - **Assisted** — the Socratic architect talks a topic out, then proposes a note
    to a **staging area** you confirm with **Apply**; it also handles quick
    additive ops ("add milk to the shopping list", "log a 5k run") that apply
    instantly with one-tap **Undo**. No destructive auto-apply — deletes/edits go
    through staging.
  - **Research** — a **read-only** Q&A over your brain (semantic/keyword search +
    a SELECT-only `query_sql`); it never modifies anything.
- **Editable prompts** — every prompt (the architect modes and the workflow AI
  actions) lives in `prompts.yaml`, hot-reloaded on change, and is editable from
  the app (**Flows → Prompts**). In-app edits are stored in the DB (so they
  survive updates); "Reset to default" returns to the shipped `prompts.yaml`.
- **Wiki** — markdown notes with `[[wiki-links]]`, automatic **backlinks**, and
  full **revision history**: every edit is versioned and attributed (you vs. the
  AI vs. a restore), with **line diffs** and one-click **restore** (which snapshots
  first, so history is never lost).
- **Attachments** — attach **any number of files, up to 100 MB each**, to a note
  (stored in the DB so backups stay complete) — both on a note's page and right from
  the compose box while capturing. **Audio & video play inline.** Searchable text is
  extracted automatically: text/code files are decoded, **PDFs** are text-extracted,
  **image EXIF/metadata** is pulled, **images** get an AI vision summary, and **audio**
  is **transcribed locally** (faster-whisper, no API key — same as embeddings) — all
  indexed for keyword + semantic search and available to the AI.
- **Search** — hybrid **keyword (FTS5)** + **semantic** (local embeddings, no
  extra API key).
- **Knowledge graph** — an interactive map of notes and their links.
- **SQL access** — a built-in read-only SQL console, plus full `sqlite3` CLI.
- **PWA** — installable, responsive (first-class phone *and* desktop layouts),
  with offline reading of notes you've already viewed.
- **Workflows** — trigger→action automations defined as repo YAML (`workflows/`),
  ingested into the DB on boot and editable in the PWA. Triggers: app **events**,
  fixed **intervals**, or **cron** (`"0 7 * * *"`, in the server timezone). The
  Claude prompt for AI actions is set in the workflow's `config`. Runs are logged;
  writes are versioned and attributed `source='workflow'`.
- **Review inbox** — workflows can post **review items** (title, message, link to
  an entry, dismiss) surfaced in a PWA **Review** tab with a count badge — e.g.
  daily-review messages — for easy visibility of what automations produced.
- **Day-log summaries** — log to a "Daily Log" throughout the day; the first
  entry of a new day auto-summarises the previous day into a "Daily Summaries"
  note and posts a review card (a built-in workflow).
- **Location & time** — entries record when they were made (always) and, with the
  opt-in 📍 toggle in the PWA, *where*: chat/quick-task/capture entries are
  stamped with your coordinates and shown on the note (with a map link).
- **Knowledge-base synthesis** — a scheduled workflow analyses the entries since
  its last run and folds their durable knowledge into a continually-updated
  **Knowledge Base** layer that links back to the source entries (auto-applied,
  versioned, posts a Review card). Browse it via **Wiki → Knowledge base**.
- **Manual editing** — edit any note's markdown directly in the PWA (versioned
  like every other change), so you can refine or correct the synthesized wiki.

## Hosting the PWA on GitHub Pages (optional)

The PWA can run from GitHub Pages instead of being served by your server — useful
to install/update the app independently of the VM.

- GitHub Pages serves *built static files*, not the `web/` source, so a workflow
  (`.github/workflows/pages.yml`) builds `web/` and publishes it. Enable it in
  **Settings → Pages → Source: GitHub Actions**; it deploys on pushes to `main`.
- Because the app is now a different origin from your server:
  - **First run asks for your server address** (your VM's `https://…`) plus the
    access key; both are stored on-device and used for every request.
  - The server allows cross-origin calls via **CORS** (`JBRAIN_CORS_ORIGINS`,
    default `*`; safe since auth is a bearer token, not cookies).
  - The app checks the **server version** against its own and shows a banner if
    they differ, so you can keep both in sync.
- Served by your server instead (the default)? Leave the server address blank —
  everything is same-origin and works as before.

## Authentication

There are no usernames or passwords. A single high-entropy **access key** (the
"cert") is the only credential:

- It's generated at install (or by the server on first run, printed to the logs
  and saved to `/data/access-key.txt`).
- You **paste it once** into the PWA on first run; it's stored on that device and
  sent as `Authorization: Bearer <key>` over HTTPS with every request. The phone
  app uses the same key; the watch holds no key — it relays dictations to the phone,
  which forwards them.
- The server stores only a SHA-256 hash and compares in constant time.
- **Rotate** the key by editing `JBRAIN_ACCESS_KEY` in `.env` and restarting; old
  devices simply re-paste the new key.

Transport is already encrypted by Caddy's TLS, so the key authenticates each
call rather than adding a second encryption layer.

## Architecture

```
            ┌────────────┐      ┌─────────────────────────────┐
 phone /    │   Caddy    │  →   │           api               │
 desktop ── │ TLS + proxy│      │  FastAPI + built React PWA   │
   PWA      └────────────┘      │  Claude · fastembed · SQLite │
                                └──────────────┬──────────────┘
                                               │  brain.db (volume)
```

- **Backend**: FastAPI (`server/`), Anthropic SDK for the architect, `fastembed`
  for local embeddings, `sqlite-vec` + FTS5 for search.
- **Frontend**: React + Vite PWA (`web/`), built into the API image and served
  as static files.
- **Proxy**: Caddy terminates TLS (automatic Let's Encrypt) for your domain.

## Quick start (Linux VM)

Prerequisites: **Docker Engine + Compose v2**, a **domain** whose A record points
at the VM (set this up *before* installing so Caddy can issue the cert), ports
**80/443** open, and **≥ 2 GB RAM** (the local embedding model loads into memory).

```bash
git clone <your-fork-url> JBrain && cd JBrain
./install.sh
```

The installer asks for your domain, Anthropic API key, brain name, and
timezone, generates a high-entropy **access key** (printed at the end — save
it), then writes `.env` + `Caddyfile` and offers to start everything. When it's
up:

1. Open `https://<your-domain>` and **paste your access key** to connect.
2. Use the browser's **Install app** / **Add to Home Screen** to install the PWA.
3. Go to **Chat** and start talking. When the architect proposes a
   **Staging area**, tap **Apply** to write notes.

Manual control:

```bash
docker compose up -d --build     # build & start
docker compose logs -f           # follow logs
docker compose down              # stop
```

> First boot downloads the local embedding model (a few hundred MB) and the local
> speech-to-text model (`AUDIO_MODEL`, ~140 MB for `base`) from Hugging Face and
> loads them into memory — both are warmed in the background, so the server is
> usable immediately and only the very first semantic search / audio transcription
> may wait. Needs runtime network egress on first boot and **≥ 2 GB RAM** (set
> `AUDIO_MODEL=tiny` on a tight box). Both run without any external API key.
>
> Running a **fork**? Set `JBRAIN_REPO=owner/name` in `.env` so the in-app update
> checker points at your repo. The auto-update sidecar (`COMPOSE_PROFILES=autoupdate`)
> mounts the Docker socket (host-root-equivalent) — leave it **off** unless you
> want PWA-triggered updates, and use `./update.sh` for manual updates instead.

## SQL access

- **In-app**: the **SQL** tab runs read-only `SELECT` / `WITH` queries.
- **Full CLI**:
  ```bash
  docker compose exec api python -c "import sqlite3;print('use sqlite3 CLI below')"
  docker compose exec api sh -c "apt-get update && apt-get install -y sqlite3" # if needed
  docker compose exec api sqlite3 /data/brain.db
  ```

## Updating

JBrain shows an **Update** banner in the PWA when a newer version exists. By
default it **tracks `main` by commit** (the image is built with its git commit
baked in, compared against the latest commit on `main`) — so you don't need to
cut releases; just push to `main`. If you prefer versioned releases, a published
GitHub Release/tag newer than `APP_VERSION` takes precedence. Updating is
**non-destructive**: the database and Caddy certs are on Docker named volumes and
your `.env` (access key, API keys) is untouched — only code is replaced, and
schema changes are applied by the migration runner on boot.

**Fully automatic (recommended):** enable the updater sidecar — answer "yes" to
automatic updates in `install.sh`, or set `COMPOSE_PROFILES=autoupdate` in `.env`
and `docker compose up -d`. Then tapping **Update** in the PWA is end-to-end: the
server writes an update request, the `updater` container pulls the latest release,
rebuilds the `api` image, and restarts it (Caddy and the updater keep running).
The updater needs the Docker socket and the project directory — that's the
trade-off for hands-off updates.

**Manual alternatives** (if you don't enable the updater):
- Run `./update.sh` (optionally `./update.sh v0.2.0`) on the host.
- Or set `JBRAIN_UPDATE_CMD` so the **Update** button runs it in the api
  container, or wire your own host watcher to the `data/update-requested.json`
  marker the server writes.

> Keep `COMPOSE_PROJECT_NAME` stable (set by `install.sh`) so the updater targets
> the same stack you launched.

Workflows can be turned on/off, edited, and **re-synced from the repo** anytime
(Workflows → *Sync from repo*); a workflow you edited shows *Reset to repo* to
track the shipped definition again.

## Backup & restore

The whole brain — notes, attachments, history, workflows — is one SQLite file.

**From the app (easiest):** **SQL/Database** tab → **Export database** downloads a
consistent snapshot (`.db`); **Import database…** replaces everything from a
backup file (it's upgraded to the current schema on import, and your configured
access key stays valid). Available at `GET /api/system/backup` and
`POST /api/system/restore`.

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
# Backend
cd server && pip install -r requirements.txt && uvicorn app.main:app --reload
# Frontend (proxies /api to :8000)
cd web && npm install && npm run dev
# Tests
cd server && pytest
```

## Configuration

All config is environment-driven (`.env`, see `.env.example`): `LLM_PROVIDER`,
`LLM_API_KEY`, `LLM_MODEL` (the legacy `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL`
still work), `BRAIN_NAME`, `JBRAIN_ACCESS_KEY`, `EMBEDDING_MODEL`,
`JBRAIN_DOMAIN`, `DB_PATH`.

## Roadmap

Multi-user accounts · voice capture in the PWA · full offline sync · native Wear
OS app · note editing UI · tag management.
