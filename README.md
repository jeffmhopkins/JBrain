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
- **Three modes** (pick in the app):
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
- **Attachments** — attach `.txt`/`.md` files to any note; their contents are
  **searchable** (keyword + semantic) and the AI can read them for grounding.
- **Search** — hybrid **keyword (FTS5)** + **semantic** (local embeddings, no
  extra API key).
- **Knowledge graph** — an interactive map of notes and their links.
- **SQL access** — a built-in read-only SQL console, plus full `sqlite3` CLI.
- **PWA** — installable, responsive (first-class phone *and* desktop layouts),
  with offline reading of notes you've already viewed.
- **Quick-capture inbox** — a tiny `/api/capture` endpoint so a phone shortcut
  or a Wear OS tile can dictate thoughts the architect folds in later.
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
  sent as `Authorization: Bearer <key>` over HTTPS with every request. The watch
  uses the same key.
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
at the VM, and ports **80/443** open.

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

> First boot downloads the local embedding model (~tens of MB) and needs a bit
> of RAM. Semantic search works without any embedding API key.

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

## Dictate from a watch / phone shortcut

Wear OS can't run the PWA, so capture goes through `POST /api/capture` with a
JSON body `{"content": "..."}` and the same access key as a header:

```
POST https://<your-domain>/api/capture
Authorization: Bearer <your-access-key>
Content-Type: application/json

{"content": "remember to follow up on the budget idea"}
```

Wire it up with a phone shortcut, a share-sheet target, or a Wear tile via
Tasker/AutoWear (add a static `Authorization` header). Captures land in an inbox
the architect reviews in your next chat. A polished native Wear OS app is a
planned v2 add-on.

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

All config is environment-driven (`.env`, see `.env.example`): `ANTHROPIC_API_KEY`,
`ANTHROPIC_MODEL`, `BRAIN_NAME`, `JBRAIN_ACCESS_KEY`, `EMBEDDING_MODEL`,
`JBRAIN_DOMAIN`, `DB_PATH`.

## Roadmap

Multi-user accounts · voice capture in the PWA · full offline sync · native Wear
OS app · note editing UI · tag management.
