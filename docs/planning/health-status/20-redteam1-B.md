# Red-Team 1 — Critique of Plan B (Dedicated Status Endpoint + Client Heartbeat)

Reviewer: adversarial. Verified against the actual code at `/home/user/JBrain`
on 2026-06-07. Verdict up front, then evidence.

---

## TL;DR verdict

Plan B is the **best-architected of the four** on the *server* side: one typed,
cached, soft-authed `/api/system/status` with a five-state vocabulary, a single
ambient heartbeat, and gating that reads the same document. Its three-axis model
(browser-offline vs server-unreachable vs degraded) is the correct mental model
and the one the goal actually asks for. **But several of its load-bearing
technical claims are wrong or badly underspecified**, and two of them are the
ones it leans on hardest: the `authHeaders()` call in the heartbeat does not
exist as an export, the `_last_auth_error` LLM hook is an order of magnitude more
invasive than the one-line framing admits, and the "polled even pre-auth on
KeyEntry" claim contradicts where the UI is actually mounted. The soft-auth
dual-depth route is feasible (the APIs it needs *do* exist) but is a genuine
leak-surface that the plan hand-waves with "one test."

It will work and it is mostly the right design — but it is not as cheap to build
or as airtight as written, and "20s polling" is not "real-time."

---

## 1. Correctness bugs (claims vs. reality)

### HIGH — `authHeaders()` is not exported; the heartbeat as written won't compile
Plan B §(c): *"poll via raw `fetch(u("/api/system/status?detail=1"),{headers:authHeaders()})`"*.
In `web/src/api.ts:34`, `authHeaders` is a **module-private** function — only
`u`, `api`, `get/post/put/del`, `streamChat`, `ApiError`, `setAccessKey`, etc.
are exported (verified: `grep export web/src/api.ts`). So the plan must *either*
export `authHeaders` (fine, one line) *or* use the existing `api()` wrapper —
but `api()` **throws** `ApiError("Not authenticated",401)` on 401 (`api.ts:45`),
which is exactly what the plan says it must not do. The plan never reconciles
this. As written it references a symbol that doesn't exist. Cost: a small but
real change to `api.ts` that the plan doesn't budget for, plus a decision about
whether to also tag network errors (which it wants for §(f) anyway).

### HIGH — `_last_auth_error` in `services/llm.py` is grossly underspecified
Plan B §A3: *"a module-level `_last_auth_error: tuple[ts,msg]|None` in
`services/llm.py`, set when a real completion errors."* Reality
(`server/app/services/llm.py`):
- There is **no single chokepoint**. Completions live in **two provider classes**
  (Anthropic ~`:163-258`, xAI/OpenAI ~`:315-358`) each with `complete`,
  `complete_with_meta`, `complete_with_tools`, plus async `stream_turn`.
- **None of them catch provider errors.** `client.messages.create(**kwargs)`
  (`:175`) and `client.chat.completions.create(...)` (`:323,336,370`) raise
  straight through to callers. To set `_last_auth_error` you must wrap *every*
  one of these in try/except, capture, set state, and re-raise — across both
  classes and both sync+streaming shapes. Streaming errors surface mid
  async-generator (`stream_turn` `:374+`), a different catch site again.
- Distinguishing "401/billing-failed" (→ `degraded`) from a transient timeout
  (→ ignore) requires **provider-specific exception typing** (anthropic SDK
  `AuthenticationError`/`PermissionDeniedError` vs openai SDK equivalents). The
  plan's "401/billing-failed" language assumes a uniform error shape that does
  not exist here.

This is not a one-liner; it's a cross-cutting change to the most sensitive
service in the app, and the plan's phase list buries it as a sub-bullet of
Phase 1. **This is the single most under-costed item in the plan.**

### MEDIUM — "polled by heartbeat even pre-auth (KeyEntry)" contradicts the mount point
Plan B §B1 says the public skeleton is *"Polled by heartbeat even pre-auth
(KeyEntry)."* But the `StatusIndicator` is *"mounted in `Shell.tsx` top bar"*
(§d), and `Shell` only renders in the **authed** branch of `App.tsx:127-159`
(`!authed ? <KeyEntry/> : ... <Shell>`). On the KeyEntry screen there is no
Shell, no indicator, and nothing starting the poller. So the public-skeleton
path — the entire justification for the soft-auth dual-depth route — has **no
caller in the UI as designed.** Either the heartbeat must start above the auth
gate (new wiring the plan doesn't describe, and which then races with `loadInfo`
/`connect`), or the public skeleton is dead weight. The plan can't have both
"indicator lives in Shell" and "polled pre-auth."

### MEDIUM — `system.py:32` is **not** a 3s-TTL pattern
Plan B §B4: *"In-process micro-cache in `snapshot()`, TTL `STATUS_TTL=3s` (like
`system.py:32`)."* `routers/system.py:32` is `_cache = {"ts":0.0,"data":None}`
guarding the **GitHub release check with a 1-hour TTL** (`00-research.md` even
says "cached 1h"). It is a fine *shape* to imitate but the citation implies a
precedent for 3s polling-cache that isn't there. Cosmetic, but it's the kind of
loose anchor that makes a reviewer distrust the rest.

### LOW — `update_sidecar` source is `os.environ`, not a settings prop
Plan B §A3 table: `update_sidecar` ready when *"`"autoupdate" in
COMPOSE_PROFILES`"*. `COMPOSE_PROFILES` is **not** in `config.py` — the only read
is `routers/system.py:253`: `"autoupdate" in os.environ.get("COMPOSE_PROFILES","")`.
The aggregator must read `os.environ` directly (as system.py does). Trivial, but
the plan implies a `settings`/config accessor that doesn't exist.

### CONFIRMED-CORRECT claims (credit where due)
- `verify_key(key)` (`auth.py:58`) and `_extract_key(request)` (`auth.py:67`)
  both exist and are importable — **the soft-auth `optional_key` dependency is
  genuinely feasible.** `verify_key(None)`/empty → `False` (no throw), exactly
  what a non-raising soft-auth needs.
- `embeddings._model`/`_model_lock` at `embeddings.py:16-17`; `_get_model`
  `:20-30`. ✔ (anchors correct)
- `audio_transcription._model`/`_model_key`/`_model_lock` at `:38-40`;
  `ImportError` branch at `:103-107`; `TranscriptionUnavailable` `:72`. ✔
- `entity_rebuild.status(conn)` `:56-65` returns the typed dict the plan cites. ✔
  (note: it **requires a `conn` arg** — the aggregator must pass one.)
- `geocode.enabled()` `:38`, `push.public_key()` `:67`, `llm.has_credentials()`
  `:506`. ✔ all exist with the claimed semantics.
- `main.py:254-256` health `{ok,brain}`; CORS `:232-242` `allow_credentials` off,
  `*` default. ✔
- `App.tsx:106` is the sole 401-logout site. ✔
- `api.ts:752` 90s stall watchdog. ✔
- `Modal.tsx` and `SystemPage.tsx` both exist (panel reuse + link are real). ✔

---

## 2. Goal gaps

### Is 20s polling "real-time"? — NO, and the plan admits it
The owner's word is "real-time." 20s steady / 15s degraded / 8s abort means a
subsystem can flip and the UI lags up to ~20s; a server death is detected in up
to 8s + interval. The plan's own Risks section concedes this ("Polling not truly
real-time"). For the `warming → ready` transition (the one transient that
matters on a fresh boot) 15s is *barely* acceptable; for "warn me **before** I
use it" it's fine on a cold page but weak mid-session. **Plan D is the only one
that actually delivers push transitions.** Plan B's "future: short SSE only
while warming" is the right hedge but it's deferred, so as shipped Plan B does
not meet the literal "real-time" bar.

### Does it warn before use for **every** subsystem? — Mostly, with two soft spots
- The gating table §(e) is broad and good (transcription, search, chat, note AI,
  rebuild/research, push, map). Better coverage than A, comparable to C.
- **LLM "ready" is presence-only.** A present-but-revoked/over-quota key shows
  green and fails at call time. The plan accepts this (cost) and backstops with a
  toast — but that is *warn-after-use*, not before. This is inherent to the
  no-token-burn constraint and is shared by A/C; Plan B's `_last_auth_error`
  *partially* closes it **only after the first failure**, never preemptively.
- **`image_analysis` is mapped to `llm.has_credentials()`** but image vision also
  needs a vision-capable model; "key present" doesn't guarantee the configured
  model can see images. Minor honesty gap.

---

## 3. Risk & robustness

### HIGH — soft-auth dual-depth route is a real leak surface
One route returning two shapes by auth state is a classic place to leak. Risks:
- The `?detail=1` switch is **client-controlled**; the gate is whether
  `optional_key` returned true. A refactor that reads `detail` before checking
  auth, or that builds the full dict then trims it, will leak. Building the full
  document and conditionally stripping is the natural (wrong) implementation.
- The **public skeleton's `overall` rollup itself leaks signal**: "degraded"
  pre-auth tells an unauthenticated observer that *some* subsystem is unhealthy —
  more than `/api/auth/info` (`auth_router.py:15`) reveals today (brain name
  only). `00-research.md §8` says *"don't leak capability details pre-auth beyond
  what auth/info already does."* A coarse `overall` arguably already crosses that
  line. At minimum the public skeleton should be `{ok,brain,ts}` with **no
  rollup**, or `overall` should collapse to liveness pre-auth.
- Mitigation offered is "one unauthed-shape test." For a security boundary that's
  thin. It needs: a test asserting the public body has an **exact** allowlist of
  keys, and a structural guarantee (build skeleton and detail as **two separate
  builders**, never strip-from-full).

Severity HIGH because it's a security regression risk on the *one* genuinely new
public surface, and the plan's whole "no router-level 401" design exists to
enable it.

### MEDIUM — scheduler heartbeat: blocking the event loop + wrong placement
Plan B §A3: *"in `_scheduler_loop` (`main.py:63`) `set_meta(conn,
"scheduler:last_beat",iso_now)` each successful iteration."* Hazards:
- The loop **sleeps 60s first, then works** (`main.py:67-68`), and each work item
  runs via `asyncio.to_thread`. If the heartbeat `set_meta(get_conn(), ...)` is
  written **inline in the async loop** (not inside a `to_thread`), it does a
  synchronous SQLite write **on the event-loop thread** — and `get_conn()` is
  **thread-local** (`db.py:82-88`), so it spins up a *new* connection bound to
  the loop thread doing blocking WAL I/O on the hot path. Must be wrapped in
  `to_thread` like every other call in that loop. The plan doesn't say where.
- It also detects a *wedged loop only if the loop is still iterating*. But the
  loop's failure mode is an item that **hangs without raising** (a stuck LLM
  action inside `run_due_scheduled`); the existing code already runs items in
  threads precisely so a hang can't freeze the loop. So a heartbeat at the top of
  the iteration keeps beating even while an action is wedged — it detects
  "asyncio loop dead" (rare) but not "scheduler effectively stuck" (the realistic
  case). Marginal value for the wiring cost; honestly, the weakest of the nine
  subsystems and a candidate to cut.
- `meta` is in `db._RO_DENY_TABLES` (`db.py:25`) — fine for the SQL console, and
  irrelevant to writes, but worth noting the heartbeat key joins a table the
  ad-hoc SQL path can't read (so it can't be inspected via /sql). No bug, just a
  diagnostic blind spot.

### MEDIUM — distributed readiness flags on the hot lazy-load path
The plan adds `_set_state`/`_state_lock` *inside* `_get_model()` in both
embeddings and audio. `audio_transcription._get_model` **reloads when config
changes** (`_model_key != want`, `:97-100`) — the plan's "set ready after _model
assigned" must handle the *reload* case (model swap in Settings GUI) or it'll
report `ready` for a stale model while a new one loads. The plan's "mirrors
`_model`/`_model_lock`" framing misses this branch. Low-probability but a
mislabel on a path users actively touch (changing whisper model in settings).
Severity MEDIUM because it's "wrong status," the exact thing the feature exists
to prevent.

### LOW — multi-worker / per-process flags
Readiness is in-process memory. Single-process today, but if JBrain ever runs
`uvicorn --workers N` behind Caddy, the soft-auth status endpoint hits a random
worker and embeddings/audio readiness flickers per worker. Same latent gap Plan A
flags honestly; Plan B's Risks section omits it. Should be stated.

### LOW — cross-origin / cost / offline-auth
- Cross-origin: fine — bearer fetch via `u()`, no cookies, matches `main.py`
  CORS. (Once `authHeaders` is actually exported.)
- Cost: genuinely cheap — in-memory reads + one `SELECT 1` + 3s cache; no token
  burn. ✔ Best-in-class on this constraint.
- Offline-auth: design correctly never calls `clearAccessKey` from the heartbeat;
  the invariant survives **provided** the poller uses raw fetch and not `api()`.
  The plan says so, but see the HIGH `authHeaders` bug — the *only* safe way is a
  bespoke fetch, which means more new code in `api.ts` than implied.

---

## 4. What Plan B does BETTER than A / C / D (keep these)

1. **Three-axis reachability model** (`browser-offline` / `server-unreachable` /
   `degraded`) is the sharpest articulation of the goal in any plan, and it's the
   one thing `useOnline` (`hooks.ts:264`) fundamentally can't do. A mentions it;
   B makes it the spine.
2. **Single dedicated `/api/system/status` with a 5-state vocabulary**
   (`ready/warming/degraded/unavailable/unknown`) is richer than A's per-subsystem
   ad-hoc enums and C's 4-state, and crucially separates "loading, retry"
   (`warming`) from "never here" (`unavailable`) — essential for honest copy.
3. **One source of truth for "what's broken" and "what's disabled"** — gating
   reads the same doc the indicator shows. C achieves similar; A bolts gating onto
   `auth/verify`. B's separation of a *new dedicated* endpoint from the
   DB-touching `/verify` (which calls `people.owner_name` + `push.public_key`
   per call) is the right cost call.
4. **`StatusCtx` decoupled from auth** — keeps the offline-tolerant auth flow
   untouched and avoids C's "two-source-of-truth temptation." Cleaner than A
   (which stuffs everything into `AuthCtx`).
5. **Heartbeat ergonomics**: pause-on-hidden, immediate-on-visible/focus/online,
   AbortController timeout, single-flight, backoff — mirrors the proven
   `ReviewBell` pattern (`Shell.tsx:34-60`). Production-grade.
6. **Honest, specific Risks section** — it names most of its own weaknesses
   (though it misses multi-worker and the reload-readiness branch).

---

## 5. What Plan B should STEAL from A / C / D

- **From D (the big one): the `warming`-only SSE, promoted from "future" to
  shipped, OR at least D's *observed-outcome* health.** D's insight is that
  `api()`/`streamChat` already see every 5xx/network/stall (`api.ts:40-57`,
  `:752`) — feeding those into the store gives near-real-time *server* health
  between polls for free, and is far cheaper than tightening the poll. Plan B's
  `_last_auth_error` is a clumsy server-side reinvention of D's client-side
  observed-LLM-degrade; **steal D's client observation instead** and you may not
  need the invasive `llm.py` surgery at all (observe the 401/5xx from the chat
  call client-side).
- **From D: the Caddy buffering constraint** (`Caddyfile.template:33-37`, only
  `/api/chat/*` is unbuffered). If B ever adds the warming-SSE, it must live
  under `/api/chat/*` or ship a Caddy change. B's doc is silent on this; it's a
  deployment landmine.
- **From C: the exhaustive feature→capability inventory** (C §(c) walks every
  route in `App.tsx:132-156`). B's gating table is good but not exhaustive;
  C's table is the maintained artifact that prevents drift, plus C's
  **`CAP_COPY` exhaustiveness test** that fails if a new capability lacks copy.
- **From C/D: `ApiError.category`** ("auth"|"network"|"unavailable"|...) so the
  toast layer and the heartbeat agree on classification — cleaner than B's ad-hoc
  `kind:"network"` tag.
- **From A: explicit acknowledgement of the multi-worker / per-process flag
  caveat** (A Risk #3) — B should add it.
- **From A: `find_spec` for audio is already in B** — but A's framing of
  `unavailable` (package missing) vs `failed` (load error) as *distinct* states
  is sharper than B collapsing both into `unavailable`. Steal the distinction;
  "model failed to download" and "feature not installed" need different copy.

---

## 6. Verdict + top-5 must-fix (ranked)

**Verdict:** Architecturally the strongest server design of the four and the
right conceptual model, but it overstates how cheap two of its pillars are and
hand-waves a real security boundary. Approve the *shape*; reject the cost
estimate and the soft-auth-by-stripping implementation. It is "near-real-time,"
not "real-time," and should either adopt Plan D's observed-outcome feed or
explicitly rename the goal it's meeting.

**Top 5 must-fix, ranked:**

1. **Fix the heartbeat's auth path (HIGH).** `authHeaders` is private. Decide:
   export it, or add a dedicated non-throwing `fetchStatus()` in `api.ts`. Until
   then the plan doesn't compile and the offline-tolerant guarantee is unproven.
2. **Re-scope or replace `_last_auth_error` (HIGH).** As written it's a
   multi-method, two-provider, sync+async, provider-specific-exception change to
   `llm.py` — 10x the stated effort. Prefer stealing Plan D's *client-side*
   observed-LLM-degrade and drop the server hook entirely, or fully spec the
   wrap-and-reraise across all six completion entry points.
3. **Harden the soft-auth route (HIGH).** Build public-skeleton and authed-detail
   as **two separate builders** (never strip-from-full); drop or collapse the
   public `overall` rollup so it leaks no more than `/auth/info`; add a test
   asserting the public body's **exact** key allowlist.
4. **Resolve the pre-auth-poll contradiction (MEDIUM).** Either move the heartbeat
   above the `authed` gate in `App.tsx` (and spec the race with `connect`), or
   admit the public skeleton has no UI caller and cut it (which then weakens the
   soft-auth rationale — fine, just be consistent).
5. **Fix the scheduler heartbeat wiring or cut it (MEDIUM).** Wrap the `set_meta`
   in `to_thread` (it's thread-local SQLite I/O that would otherwise block the
   event loop), place it correctly relative to the 60s leading sleep, and accept
   it only detects a dead loop (not a wedged action). Given the marginal value,
   cutting it is defensible; if kept, also fix the audio readiness **reload**
   branch so a Settings model-swap doesn't report stale `ready`.
