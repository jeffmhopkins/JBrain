# R1 — The live "Rebuild page now" engine backbone

Research-only trace of the current live rebuild machinery, written to ground the new
"Suggest revisions" conversational mode. Every important claim cites `file:line`.

Files in scope:
- `server/app/services/rebuild_engine.py` — the two-stage engine (gather → draft) + Guide/redraft.
- `server/app/routers/rebuild.py` — endpoints, the SSE bridge, run lifecycle, accept/staleness/lock.
- `server/app/services/rebuild_runs.py` — the in-memory run object + registry.
- `server/app/services/wiki_build.py` — `rebuild_sources`, `finalize_rebuild`, `build_write_prompt`, source loading, link/citation hardening, the KB write lock.
- `web/src/components/RebuildPanel.tsx` — the SSE consumer (informs the event protocol + UX states).

---

## 1. Architecture in one paragraph

A rebuild is a **session-only, in-memory** object (never persisted, never shared —
`rebuild_runs.py:1-9`). It runs in **two LLM transcripts that never mix**:

- **Stage 1 GATHER** (`rebuild_engine.py:119-209`): a *cheap* model with **tools, no
  thinking** seeds from deterministic sources, calls `search_notes`, and finishes with
  `propose_sources`. Its transcript is the local `msgs` list (`:152`), **thrown away**
  after the proposal — it never becomes `run.messages`.
- **Stage 2 DRAFT** (`run_draft` `:401-437` → `_generate` `:271-398`): a *synthesis*
  model with **thinking, no tools** writes the whole article from only the curated
  sources. This transcript **is** `run.messages` (`:435`) and is what Guide/redraft
  resume.

The module docstring states the rationale explicitly: keeping gather (tools, no
thinking) and draft (thinking, no tools) on **separate transcripts** means the
draft/Guide resume "has no tool_use blocks to preserve — trivially safe"
(`rebuild_engine.py:9-15`). **This is the single most important design fact for the new
mode** (see §7).

---

## 2. The RebuildRun object schema (`rebuild_runs.py:27-49`)

| Field | Type / default | Meaning |
|---|---|---|
| `run_id` | `str` | Opaque id, `secrets.token_urlsafe(12)` (`:99`). |
| `slug` | `str` | Page slug; unique key for **one active run per page** (`:53`,`:91-93`). |
| `title` | `str` | KB article title. |
| `model` | `str \| None` | **Pinned at creation** (synthesis model); Guide must reuse it (`:34`, set at `rebuild.py:157`). |
| `base_hash` | `str` | sha256 of the **live page content at start** — the staleness guard for Accept (`:35`,`:100`). |
| `instructions` | `str \| None` | Unused in the live path today (carried but not threaded). |
| `messages` | `list[dict]` | **The Stage-2 transcript** — verbatim provider blocks (`:37`). Built by `run_draft`, appended to by Guide/auto-continue, trimmed by redraft. |
| `known` | `list[str]` | Allowed cross-link target titles (`_known_titles`), the dead-link allow-set (`:38`). |
| `candidates` | `list[dict]` | Gathered sources `{note_id,title,date,reason,on,private,added}` (`:39`). |
| `skipped` | `list[dict]` | Considered-but-skipped `{note_id,title,date,reason}` (`:40`). |
| `draft` | `str` | The streamed/guided article body — **staged, never live** until Accept (`:41`). |
| `thoughts` | `str` | Accumulated extended-thinking text, owner-only (`:42`). |
| `sources` | `list[dict]` | `[{title}]` the rebuild loaded — used for citation repair + redraft grounding (`:43`, set `rebuild_engine.py:432`). |
| `talk` | `list[dict]` | Article-talk items to record on Accept (`:44`). |
| `status` | `str` | State machine value (see §3). |
| `error` | `str \| None` | Last error message. |
| `cancelled` | `bool` | **Cooperative cancel flag**, polled by the engine (`:47`). Set by `drop()` (`:140`). |
| `created_at` / `touched_at` | `float` (monotonic) | Sliding TTL bookkeeping. |

**Registry** (`rebuild_runs.py:52-154`): `_RUNS` (id→run), `_BY_SLUG` (slug→id, enforces
one run/page). `_TTL_SECONDS = 1800` idle, sliding — every `get()`/`touch()` resets it
(`:18-20`,`:106-119`). `_MAX_RUNS = 8` hard cap (`:21`,`:94-98`). `create()` drops any
prior run for the slug (`:91-93`). `drop()` is idempotent and sets `cancelled=True`
(`:131-142`). `content_hash()` is the sha256 helper (`:56-65`). `is_live()` = status in
`("streaming","ready","guiding")` (`:24`,`:145-154`).

---

## 3. The run state machine

`status` lives on the run (`rebuild_runs.py:45`). Transitions (engine + router):

```
            create()                run_gather                  (curate screen,
  (none) ───────────► "streaming" ──────────────► "gathering" ──► no status change)
                       (default                   set :137         │ regather loops
                        on create)                                 │ back to "gathering"
                                                                   ▼
                                                  run_gather end: "sources_ready" (:208)
                                                                   │ POST /draft
                                                                   ▼
   run_draft → _generate: "streaming" (:294) ──────► "ready" (:395, on done)
                                                                   │
                       ┌───────────── POST /guide → run_guide: "guiding" (:454) ───┐
                       │                                                            │
                       ▼                                                            │
            _generate: "streaming" (:294) ──────► "ready" (:395) ◄──────────────────┘
                       │
                       │ POST /accept  (allowed from "ready" or "guiding", router :345)
                       ▼
                 "accepting" (:358) ──► "accepted" (:381) ──► drop()  [success]
                       │
                       └─ on guard failure → back to "ready" (:361,:368,:373)

   POST /reject  → drop() (router :386-397); cancel flag stops any live generator.
   error anywhere → status "error" (engine :137,:351; rebuild_runs default list :45).
   idle > TTL → _sweep() drops it (skips "accepting") (:68-73).
```

Notes:
- `_generate` sets `status="streaming"` even inside a Guide turn (`:294`); the run is
  briefly "guiding" only between `run_guide` entry (`:454`) and `_generate` start. The
  frontend tracks its own finer `Phase` (`guiding-streaming` etc., `RebuildPanel.tsx:16`).
- **Accept is gated on `status in ("ready","guiding")`** (`rebuild.py:345`) and a
  non-empty draft (`:347-348`).
- `_LIVE = ("streaming","ready","guiding")` (`rebuild_runs.py:24`) gates Guide (`rebuild.py:308`).
- `_sweep()` deliberately **never reaps an "accepting" run** (`rebuild_runs.py:71-72`) —
  protects the in-flight commit.

---

## 4. The SSE event protocol

### Transport bridge `_sse` (`rebuild.py:57-111`)
A background `pump()` task drains the async event generator into a queue; the stream
loop reads with a `_SSE_KEEPALIVE_SECONDS = 15.0` timeout (`:24`,`:90`), emitting
`": keepalive\n\n"` comments during silent thinking stretches (`:92`). Each event is
written as `event: {type}\ndata: {json}\n\n` (`:99`). Pump exceptions become a generic
`event: error` (`:80-82`,`:97`) — detail is logged server-side, never leaked. This
mirrors the chat SSE bridge.

### Event vocabulary (every `type` emitted)

| `type` | Emitted by | Payload keys | Consumer handling |
|---|---|---|---|
| `run_started` | `rebuild.py:161` (start only) | `run_id, slug, title, base_rev` | stores `runId` (`RebuildPanel.tsx:84`) |
| `tool_use` | `rebuild_engine.py:173,189` | `tool`, `query?` | adds a running step (`:85-88`) |
| `tool_result` | `rebuild_engine.py:182` | `tool, summary, items` | resolves the step (`:89-98`) |
| `sources_proposed` | `rebuild_engine.py:209` | `candidates, skipped` | → curate stage (`:99-104`) |
| `thinking_delta` | `rebuild_engine.py:328` | `text` | appends thoughts (`:119`) |
| `content_delta` | `rebuild_engine.py:332` | `text` | appends draft, first one flips to "Drafting…" (`:120-123`) |
| `lint` | `rebuild_engine.py:381,390` | `ok, message` | shows warn banner (`:124`) |
| `done` | `rebuild_engine.py:396` | `draft, truncated, lint{ok,errors,warnings,stub}` | final draft + phase (`:125-131`) |
| `error` | `rebuild_engine.py:138,353` + bridge `:97` | `message` | error phase (`:105,:132`) |

The same `done`/`content_delta`/`thinking_delta`/`lint`/`error` set is shared by
`/draft`, `/guide`, `/redraft` (all funnel through `_generate`). Only `/start` (+
`/regather`) emit the gather events. **There is no diff/patch event type today** — the
draft arrives only as a full-body stream of `content_delta`s plus a final `done.draft`.

---

## 5. How `run.messages` is threaded; thinking + tool_use handling

- **Stage 1** uses a **local** `msgs` (`rebuild_engine.py:152`); tool results are
  appended via `provider.append_tool_results` (`:193`). It is discarded after gather.
  `run.messages` is never touched here.
- **`run_draft`** *resets* the transcript to a single user turn = the writer prompt:
  `run.messages = [{"role":"user","content": build_write_prompt(...)}]` (`:435`).
- **`_generate`** streams from `run.messages` with `tools=[]` and `thinking=True`
  (`:320-321`). `stream_turn` **appends the assistant turn verbatim (signed thinking
  blocks included)** to `run.messages` — this is the documented basis for safe auto-continue
  (`:299-308`). Thinking deltas accumulate into `run.thoughts` (`:327`); text deltas into
  local `parts` (`:331`) and ultimately `run.draft` (`:394`).
- **Auto-continue** (`:309-358`): if `TurnEnd.stop_reason in ("max_tokens","length")`
  (`:338`) the engine appends a `CONTINUE_PROMPT` user turn (`:311`,
  `wiki_build.py:45-52`) and streams **one** more turn with thinking OFF (`:314`),
  accumulating into the same `parts`. Capped at one continuation (`for ... range(2)`);
  still-truncated surfaces `truncated=True` (`:389-391`).
- **`run_guide`** (`:440-463`): appends ONE user turn containing a `steer` instruction
  (`:455-461`) to the existing `run.messages`, then re-runs `_generate`. **Because the
  Stage-2 transcript carries no tool_use blocks**, this resume is safe — exactly the
  invariant the module docstring relies on (`:9-15`).
- **`run_redraft`** (`:466-503`): pops the trailing truncated assistant turn (`:491-492`)
  and unwinds any `[CONTINUE_PROMPT user, partial assistant]` auto-continue scaffolding
  (`:496-501`), so `run.messages` again ends with the original prompt — then re-runs
  `_generate` at a bigger budget. Works for both initial draft and a Guide turn.

**Token budgets:** Stage-2 default `_MAX_TOKENS = 6000`, ceiling `_MAX_TOKENS_CEILING =
16000` (`:29-30`), clamped by `_clamp_tokens` (`:33-48`). Gather: `_GATHER_MAX_ITER = 5`,
`_GATHER_MAX_TOKENS = 1500`, `_GATHER_SEARCH_LIMIT = 8` (`:50-52`).

---

## 6. Why a Guide turn REWRITES the whole article (the crux for the new mode)

`run_guide`'s steer prompt literally instructs: *"Output the COMPLETE revised article in
the same Markdown format"* using only the already-provided sources
(`rebuild_engine.py:455-461`). Consequences:

- The model regenerates the **entire body** every turn; `_generate` sets `run.draft = ""`
  at the start (`:295`) and streams a fresh full document.
- All hardening (dead-link lint, citation repair, `add_links_to_content`, structure
  validation) re-runs on the **whole** new draft (`:366-398`).
- The frontend reflects this: Guide clears `draft` and re-streams from scratch
  (`RebuildPanel.tsx:233`), and the curate-stage copy promises "I'll rewrite from your
  chosen sources (no new lookups)" (`:466`).

So today's "conversation" is really **N independent full re-drafts sharing one growing
transcript** — not targeted edits. The transcript accumulates each full prior draft as an
assistant turn plus each steer as a user turn; cost and latency grow per turn.

---

## 7. Context loaded today vs. what the new mode needs

**`rebuild_sources`** (`wiki_build.py:1640-1678`) is the seed-source resolver, shared by
nightly + live: sources = the article's prior **non-kb** citations ∪ the **entity index**
for the subject (`:1660-1668`); search is never a seed. It also folds open
**directive/conflict** article-talk into `instr` (`:1669-1674`) and derives `scope` from
the H1 (`:1675`). The gather agent seeds from `art["sources"]` (`rebuild_engine.py:141-147`).

**Sources are loaded** by `_load_sources` (`wiki_build.py:412-462`): trimmed to
`SOURCE_BUDGET`, **RAW content — never expands `@t[...]` tokens** (`:415,:441-444`),
folds in attachment text. Rendered by `_sources_text` (`:465-474`). The full writer
prompt is assembled in `build_write_prompt` (`:784-818`).

**Backlinks (NEW context the brief wants) are NOT loaded today.** The inbound-link
pattern already exists elsewhere — `architect.py:818-824` does
`SELECT … FROM links l JOIN notes n ON n.id = l.source_note_id WHERE l.target_note_id = ?`
(and `notes.py:623-625`). The `links` table has `source_note_id` / `target_note_id` /
`target_title`. So the new mode can reuse this exact query to inject backlinking kb
articles as **read-only** context, but it is brand-new plumbing for the rebuild engine.

---

## 8. Accept / staleness / lock path (`rebuild.py:322-383`)

1. Guard `status in ("ready","guiding")` and non-empty `draft` (`:345-348`).
2. Validate opt-in `rename_to` (must stay under `kb/`) (`:351-355`).
3. **CAS-ish claim**: set `status="accepting"` *before* any DB work (double-accept guard) (`:358`).
4. Acquire the **KB write lock** `wiki_build.kb_lock_acquire` (`:360`, lock impl
   `wiki_build.py:1591-1638`, default key `kb_write`, ttl 1800s); on fail → rollback to
   `"ready"` + 409 (`:361-363`).
5. Inside the lock: re-fetch current note; if gone → `"ready"` + 409 (`:365-368`).
6. **Staleness guard**: `content_hash(current.content_md) != run.base_hash` → `"ready"` +
   409 `"stale"` (`:369-373`). The frontend renders the stale state on this 409
   (`RebuildPanel.tsx:220-222`).
7. `wiki_build.finalize_rebuild(conn, title, run.draft, run.talk, prior_note_id, rename_to)`
   then commit (`:374-376`); lock released in `finally` (`:378`).
8. `status="accepted"`, `drop(run_id)`, return `{ok, slug}` (`:381-383`).

**`finalize_rebuild`** (`wiki_build.py:1681-1721`): upserts revive-in-place (keeps slug +
version history), optionally renames an id-targeted write (`:1704-1709`), re-points
inbound dangling links (`resolve_dangling_links`, `:1714-1715`), records talk
(`:1716-1717`), rebuilds the entity index + disambiguation pages (`:1718-1719`), sweeps
dead links (`:1720`). This is the single write the new mode would also call on Accept.

**Reject** (`rebuild.py:386-397`): just `drop(run_id)`; the cancel flag stops any live
generator.

---

## 9. Deterministic hardening already in `_generate` (parity to fold in)

Applied to the assembled draft before `done` (`rebuild_engine.py:366-398`):
1. `_extract_talk` / fence stripping on the joined raw text (`:362-366`).
2. Dead-link lint vs. `allowed = run.known ∪ {title}` (`:367-368`).
3. **Citation typo repair** — near-miss `[[title]]` corrected to a curated source title
   (`_repair_citation_titles`, `:372-374`).
4. Neutralize remaining dead links + record talk notes (`:375-382`).
5. **Deterministic add-link backstop** `add_links_to_content` — in-memory, links bare
   mentions to existing kb pages, PII-firewall self-guarded (Reference/private targets
   refused) (`:387`, impl `wiki_build.py:711-781`).
6. Truncation lint (`:389-391`) + `wiki_guides.validate_structure` (`:392`).

Note: `@t[...]` date tokens are **passed through raw** (sources at
`wiki_build.py:441-444`); there is **no deterministic `@t[...]` enforcement nor explicit
people-link enforcement step** in `_generate` today beyond `add_links_to_content`. The
brief's "folded-in hardening" (date-token + people-link enforcement) is therefore NEW
work that both rebuild and the new mode would share.

---

## 10. Extension points, reuse opportunities, risks (for the targeted-edit loop)

### Reuse (large)
- **Whole run lifecycle**: `rebuild_runs` (create/get/drop/TTL/one-per-slug/cancel),
  `_sse` bridge, accept/staleness/lock, `finalize_rebuild` — all reusable as-is. A new
  mode can be a sibling run "kind" or a flag on `RebuildRun`.
- **Context gather**: `run_gather` + the curate screen + `_load_sources` + the
  candidate/skip model transfer directly. BASE-preservation just means seeding the
  working draft with the current article instead of an empty one.
- **The hardening block** (`_generate:366-398`): apply identically to an edited draft.
- **Backlinks query**: reuse `architect.py:818-824`'s inbound-link SQL to add read-only
  backlink context.

### Extension points
- **`run.messages` is the natural transcript spine.** The brief's "keep one transcript,
  append edit instructions" maps cleanly onto today's Guide threading
  (`run_guide:455-461` appends one user turn per step). The new mode wants the SAME
  append-one-user-turn-per-turn pattern — the difference is the *instruction text* and
  *what the model returns*.
- **A targeted-edit turn differs from `run_guide` in the contract**: instead of "output
  the COMPLETE revised article" (`:455-461`), it would ask for **edits** — either
  (a) a tool/JSON of find→replace / section ops applied deterministically to a working
  draft, or (b) a constrained "emit only changed sections" format. Either way you need a
  **new event type** (e.g. `edit`/`patch`) since today only `content_delta` (full stream)
  + `done.draft` exist (§4).
- **Working draft vs. regenerate**: today `_generate:295` wipes `run.draft` each turn.
  For targeted edits you want to **carry `run.draft` forward** and mutate it — add a
  "working draft" notion (seed = current article) and an apply-edits step instead of a
  full restream. This is the biggest behavioral divergence.
- **`base_hash` / staleness / Accept** need no change — they key off the live page, not
  the draft origin.

### Risks / gotchas
- **Thinking + tool_use in one transcript.** If targeted edits use **tools** (e.g. an
  `apply_edit` tool) AND thinking, you reintroduce exactly the tool_use+thinking-block
  preservation problem the current design avoids by separating transcripts
  (`rebuild_engine.py:9-15`). Resuming a transcript that mixes signed thinking blocks and
  tool_use is the known fragility. Mitigations: keep edits **tool-less** (structured text
  the server parses) like draft does, OR run edits **thinking-off**, OR keep a separate
  edit transcript. Decide this early.
- **Auto-continue / redraft scaffolding** (`CONTINUE_PROMPT`, `run_redraft:491-501`)
  assumes the transcript ends with a regenerated full draft. A targeted-edit turn that
  returns a small patch won't truncate the same way; the redraft-unwind logic may not
  apply and may need a parallel path.
- **Hardening on partial edits.** `_bad_links` / `add_links_to_content` /
  `validate_structure` currently run on a *whole* fresh draft. Applied to an edited
  working draft they still work (they take the full body), but `_repair_citation_titles`
  keys off `run.sources` (curated set) — backlink-only context articles must NOT be
  injected as curated sources or they'd become citation-repair targets and shift grounding.
- **Cost/latency** of today's full-rewrite-per-turn is the very thing the new mode aims
  to fix; but a working-draft-edit loop trades that for **drift risk** (the draft diverging
  from BASE over many turns) and **stale-transcript growth** (each turn still appends to
  `run.messages`). Consider periodically resyncing the transcript's notion of "current
  draft" to the actual `run.draft`.
- **One run per slug** (`rebuild_runs.py:53,:91-93`): the new mode and classic rebuild
  can't be live simultaneously on the same page — fine, but the UI must reflect it.
- **Frontend** `RebuildPanel.tsx` is tightly coupled to the gather→curate→draft stages
  and the current event set; a new mode needs its own panel/flow or a significant branch
  (the `Phase` enum at `:16` and `handleDraft` `:117-134` would gain an edit path).
