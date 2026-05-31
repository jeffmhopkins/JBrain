# JBrain Roadmap / Future Work

Notes captured during design. Each item includes enough context to pick up and
implement later. Items reflect the adversarially-reviewed, de-risked approach.

---

## 1. Self-update from GitHub releases (via the PWA)

**Goal:** the server can update itself to a newer release when the user approves
from the PWA — **non-destructively to the SQL database**, and **without changing
the access key or any tokens**.

**Behaviour:**
- Server knows its own version (e.g. a `VERSION` constant / `meta.app_version`).
- It periodically (and on PWA load) checks the **latest GitHub release** for
  `jeffmhopkins/jbrain` via the releases API.
- If a newer release exists, the PWA shows an **"Update available → vX.Y"** prompt.
- On user approval (key-gated `POST /api/system/update`), the server updates
  itself and restarts.

**Design notes / constraints:**
- **Data safety:** the SQLite DB lives on the `brain-data` Docker volume and is
  never touched by an update. Schema changes are handled by the existing
  **migration runner** (`db.py::_run_migrations`) on the next boot — that's
  exactly why Phase 0 built it. New versions must only add *forward, idempotent,
  guarded* migrations.
- **Secrets preserved:** `.env` (access key, Anthropic key, session/domain) is a
  bind-mounted/persistent file and is NOT regenerated. The access key lives only
  as a hash in `meta`; updates must not reseed it. `ensure_access_key()` already
  no-ops when a key/hash exists.
- **Update mechanism options (pick one):**
  1. *Docker image pull*: publish a tagged image per release; update = `docker
     compose pull && docker compose up -d`. Cleanest, but the container can't
     restart itself — needs a tiny host-side helper (a watcher container with the
     docker socket, or a systemd path unit) triggered by a flag file the API
     writes. Document the trade-off (docker socket access = privilege).
  2. *Git + rebuild*: the repo is cloned on the host; update = `git fetch && git
     checkout <tag> && docker compose up -d --build`. Same self-restart problem.
  3. *In-place for source deploys*: pull new code, run migrations, `systemctl
     restart`. Only for bare-metal installs.
- **Verification & rollback:** verify the release tag/signature; keep the prior
  image/commit so a failed boot can roll back. A health check gate before
  declaring success.
- **Endpoints:** `GET /api/system/version` (current + latest + update_available),
  `POST /api/system/update` (key-gated, kicks off the update).
- **Security:** updates are a privileged action — must be access-key gated, and
  ideally require an explicit confirm in the PWA (never automatic). Treat the
  GitHub release payload as data; pin to the known repo.

---

## 2. Day-log auto-summarisation workflow

**Goal:** build up granular log entries throughout a day; when the **first log of
a new day** arrives, summarise the **previous day's** entries into a single
"day summary" note/section.

**Behaviour:**
- `log_entry` (already shipped in Phase 2) appends dated bullets to a log note.
- On the first `log_entry` whose date is newer than the last seen log date,
  trigger a summarisation pass over the *previous* day's entries.
- Produce a concise summary (via Claude) and store it — e.g. collapse that day's
  bullets under a `## YYYY-MM-DD — Summary` heading, keeping raw entries in a
  collapsible/archive section, or write a dedicated `Daily Summary` note.

**Design notes:**
- **Trigger:** detect the day rollover by comparing the new entry's date to the
  max date already present in the log note (or a `meta` watermark per log).
- **Summarisation:** a server-side call to Claude with the prior day's bullets;
  prompt for a tight bullet summary. Runs in the background (don't block the
  user's log call).
- **Versioning:** the rewrite goes through `upsert_note`, so it's versioned and
  undoable automatically; the summary is attributed `source='workflow'` (add to
  the source vocabulary).
- **Idempotency:** guard so re-runs don't double-summarise (watermark the last
  summarised date in `meta`).
- This is the first concrete instance of the **workflow system** below.

---

## 3. Workflow authoring & deployment system

**Goal:** a clean way to define recurring/triggered workflows (like the day-log
summariser) and **push them to the server easily**.

> Note: there is no built-in "Workflow" tool in the Claude Code environment —
> this is a JBrain feature to build, not an existing capability.

**Concept:**
- A **workflow** = a trigger + an action. Triggers: schedule (cron-like), event
  (on log rollover, on note tagged X, on inbox item, on attachment upload), or
  manual. Actions: summarise, reorganise, tag, notify, call Claude with a prompt,
  etc.
- A **registry** of workflows in the DB (`workflows` table: id, name, trigger
  spec, action spec/prompt, enabled, last_run) plus a small **runner** (an async
  loop / APScheduler-style scheduler, or event hooks fired inside `upsert_note`
  and the apply path).

**"Push them to the server easily" — options:**
- *Repo-based (recommended for v1):* workflows live as declarative files
  (YAML/JSON or small Python) in a `workflows/` dir; deployed with the normal
  release/update mechanism (#1). Versioned in git, reviewable.
- *PWA editor:* a UI to create/edit workflows (name, trigger, Claude prompt),
  stored in the DB. Most ergonomic, more build effort; needs careful sandboxing
  since a workflow can drive writes.
- *Hybrid:* built-in workflows ship in the repo; user-defined ones live in the DB
  via the PWA editor.

**Safety:**
- Workflows that mutate notes must respect the same guardrails as the architect:
  additive auto-apply vs. confirm-gated; everything versioned via `upsert_note`;
  Claude prompts treat note/inbox/attachment content as untrusted data.
- Rate-limit and log every workflow run (audit trail), with an enable/disable
  switch per workflow.

---

## Already-planned (from the earlier de-risked plan, not yet built)

- **Phase 3 — Organisation:** populate `tags` + a `set_tags` tool; `notes.kind`;
  **Master Index as a read-time view** (NOT a materialised, rebuilt-on-write note
  — that was explicitly rejected for O(N) writes + search/graph pollution).
- **Auto-apply, more ops:** `complete_item` / `remove_list_item` — must use
  **exact (not fuzzy) matching, fail closed** (fall back to a staged proposal on
  any ambiguity), and stay **confirm-gated** since they mutate existing lines.
- **Performance:** make embedding refresh **async / skippable** for list/log
  appends so auto-apply never stalls the SSE stream (FTS stays synchronous).
- **Deferred footguns (low ROI):** note **rename** (multi-table link/slug/alias
  cascade), `note_aliases` canonicalisation, word-level diff refinement.
- **Watch/voice:** native Wear OS capture app; voice-to-text in the PWA.
- **Multi-user** accounts; **full offline sync** with conflict resolution.
