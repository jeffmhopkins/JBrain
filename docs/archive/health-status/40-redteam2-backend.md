# Red Team 2 — Comparative BACKEND critique (round 2, v2 plans)

**Reviewer:** Red Team (backend) · **Date:** 2026-06-08 · **Scope:** backend only,
verified against `server/app/...`. Goal: find remaining flaws across the four
converged v2 plans and pin down the single best convergent backend design for a
hybrid.

**Verdict up front:** all four have converged hard and the backend surface is now
small and largely correct. The remaining issues are a **genuine thread-safety
bug shared by A/B/C** (and mis-analyzed by D), one **search.py fix that is correct
but needs a tiny refinement**, anchor drift on the Dockerfile, and a couple of
endpoint-shape judgement calls. No HIGH security regressions remain.

---

## Code re-verification (what's actually true)

Confirmed against the tree on this pass:

- **Embeddings** `services/embeddings.py:16-30`: `_model`, `_model_lock`, lazy
  `_get_model`, `fastembed` imported *inside* the lock (`:25`). **Never reloads.** ✔
- **Audio** `services/audio_transcription.py:38-40, 93-110`: `_model`,
  `_model_key`, `_model_lock`; `_get_model` **reloads** when
  `_model is None or _model_key != want` (`:98`), `want = (audio_model(),
  audio_compute_type())` (`:97`, DB-meta overridable `:46-53`); `ImportError →
  TranscriptionUnavailable` (`:101-107`); `_model_key` set only after a
  successful `WhisperModel(...)` (`:108-109`). ✔
- **Warmers** `main.py:177-215`: both `asyncio.create_task` →
  `await asyncio.to_thread(_get_model)`; errors swallowed. ✔
- **Scheduler** `main.py:63-103`: leading `await asyncio.sleep(60)`, then every
  unit of work via `asyncio.to_thread(lambda: ...(get_conn()))`; per-item swallow. ✔
- **search.py:80-92**: the two semantic calls (`semantic_search`,
  `semantic_search_attachments`) are **bare — no try/except**, unlike every other
  branch (keyword notes `:37-48`, keyword attachments `:50-64`, keyword entities
  `:69-78`, semantic entities `:94-103`, all wrapped). **Plan C's bug claim is
  correct.** ✔
- **auth.py**: `verify_key(None|"")→False` no throw (`:58-64`);
  `_extract_key(request)` bearer-or-`X-JBrain-Key`, returns `None` (`:67-71`);
  `require_key`/`CurrentUser` is the hard-401 dependency with per-IP throttle
  (`:89-111`). Soft-auth via `verify_key(_extract_key(req))` is sound. ✔
- **/api/auth/verify** (`auth_router.py:22-38`) also runs
  `people.owner_name(get_conn())` and `push.public_key()` per call → a separate
  cheaper `/status` is justified. ✔
- Helpers exist as cited: `share.llm_ready()` (`share.py:197`),
  `entity_rebuild.status(conn)` (**takes conn** — `entity_rebuild.py:56`),
  `push.public_key()` (`push.py:67`), `geocode.enabled()` (`geocode.py:38`),
  `llm.has_credentials()` (`llm.py:506`). ✔
- **CORS** `main.py:232-242`: `allow_credentials` OFF, origins default `*`,
  bearer-only; cross-origin status poll is safe. ✔
- **Single worker**: `server/Dockerfile:45` →
  `CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]`, **no
  `--workers`**. ✔ — **but the path is `server/Dockerfile:45`, NOT `Dockerfile:45`.**
  All four plans cite `Dockerfile:45`. Cosmetic, but every plan repeats the wrong
  anchor (LOW).

---

## Area 1 — Readiness state machines

### 1a. Audio model-RELOAD branch — ALL FOUR now handle it. ✔ (was the round-1 bug)

Every v2 keys audio readiness off `_model_key == want` rather than a one-shot flag:

- **A** (`§1b`): `readiness()` recomputes `want` and, if `state=="ready" and
  _model_key != want`, downgrades to `warming`. Correct and explicit.
- **B** (`§A2`): same, returns `ready` only when `_model is not None and
  _model_key == want`. Correct; also returns `model`/`compute_type`.
- **C** (`§a3`): `if _model is not None and _model_key == want: ready; else
  warming`. Correct, minimal.
- **D** (`§a.2`): `readiness()` compares live `want` vs `_model_key`. Correct.

**No remaining reload-branch defect in any plan.** This round-1 HIGH is fully closed.

One subtlety none of them states but all get right by construction: because
`_model_key` is assigned **after** a successful load (`:109`), a *failed* reload
leaves the **old** `_model`/`_model_key` in place. So readiness will read the old
key ≠ new want → `warming` (A/B/C/D) or `failed` (A/B if the wrap catches) — both
acceptable; nobody falsely reports `ready`. Good.

### 1b. THREAD-SAFETY — a real shared bug in A/B/C, and D's analysis is WRONG. **[MEDIUM]**

The warmers call `await asyncio.to_thread(_get_model)` (`main.py:180, 211`).
A/B/C all wrap `_set_state(...)` **inside `_get_model`**. Therefore on the boot
warm path, **`_set_state` executes in the `to_thread` worker thread**, not on the
event loop. It is *also* called from the request hot path (a transcribe/search
request runs `_get_model` synchronously inside the route, on the threadpool).

Consequences:

- **Plan D is factually wrong here.** D's §5 / §a.2 claims "the `set_state` after
  the await is on the loop … no worker-thread `set_state` call site exists" and
  uses that to justify deleting all locking machinery. That is only true if state
  is set in the *warmer wrapper after the await*. But D *also* says (§a.2) "the
  warmer calls `set_state` from that [readiness()]" and wants the in-service
  `readiness()` to do the keying — which means the actual transition still has to
  be recorded somewhere that the request path also hits. If D records state only
  in the warmer-after-await, then a **cold first request that triggers the load
  before/instead of the warmer** (warmer can fail, or a request can race it)
  never updates state → stuck `warming`/`unknown`. So D must *also* set state
  inside `_get_model` (worker/threadpool), reintroducing exactly the cross-thread
  write it deleted the lock for. **D's "delete the lock, it's all on the loop"
  conclusion is unsafe.** Keep a lock.

- **Is a plain dict + lock safe? Is read-during-write safe?** For a single
  worker, CPython's GIL makes a single dict-field assignment atomic, but the
  readiness *snapshot* reads several fields (`state`, `last_error`, `since`/
  `model`) and could observe a torn pair (e.g. new `state` with stale
  `last_error`). The fix is what **B and D already do**: guard both `_set_state`
  and the snapshot read with the **same `threading.Lock`** (B `§A1`: `_state_lock`
  around both; D `§a.1`: `_lock` around `set_state` and `snapshot`). **A and C do
  NOT show a lock around their readiness state** — A's `_set_state` mutates three
  globals unguarded and `readiness()` reads them unguarded; C's `readiness()`
  reads `_model`/`_model_key`/flags unguarded. In practice the tear is cosmetic
  (a one-poll-stale `last_error`), so **MEDIUM, not HIGH** — but the convergent
  design should adopt B/D's explicit lock for correctness and to keep the
  "someone adds `--workers`" footgun honest.

- **Reading `_model_key`/`_model` from `readiness()` while `_get_model` writes
  them** (audio): these are written under `_model_lock` but `readiness()` reads
  them *without* taking `_model_lock` (all four). A read can see `_model` updated
  before `_model_key` (or vice versa) within the lock'd section. Worst case: a
  single poll reports `warming` for one extra tick. **LOW**, acceptable, but worth
  a one-line comment; do **not** take `_model_lock` in `readiness()` (that would
  let a poll block behind a multi-hundred-MB model load — the opposite of the
  "cheap, never blocks" requirement).

### 1c. Does wrapping `_get_model` change hot-path behavior? — Essentially no. **[LOW]**

The wraps add `_set_state(...)` calls and (A/B) a try/except that re-raises. The
return value and raise semantics are preserved in all four. Two nits:

- **A's embeddings wrap** moves the `from fastembed import TextEmbedding` import
  inside a try that sets `failed` then `raise`. Fine — but note the import is
  already inside the lock (`:25`), so behavior is identical; the only change is a
  caught-and-rethrown exception now carries a truncated `last_error`. Safe.
- The `_set_state` call inside the lock adds a (tiny) extra lock-hold on the audio
  path. Negligible. No hot-path regression in any plan.

**Net:** the wrap is behavior-preserving in all four; the only real defect is the
**missing lock around the readiness tuple in A and C** (MEDIUM) and **D's
incorrect "no lock needed" reasoning** (MEDIUM).

---

## Area 2 — Status endpoint design

Three distinct shapes:

- **A:** extend `/api/auth/verify` (initial snapshot rides free) **+** a new
  cheap key-gated `GET /api/auth/status` (`CurrentUser`). Both **hard-authed**.
- **B / D:** a new **soft-auth** `GET /api/system/status` on a dedicated router;
  unauthed → liveness skeleton `{ok,brain[,ts]}`; authed → full snapshot.
- **C:** new key-gated `GET /api/capabilities` (`CurrentUser`, hard-auth) +
  server-driven `llm_ready` boolean on the public share landing.

### 2a. Is any version still leaking a public skeleton? — No HIGH leak remains. ✔

The round-1 HIGH (B v1's public `overall` rollup) is fixed. B v2 (`§B1`) returns
an **exact, two-builder, allowlist-tested** `{ok,brain,ts}` pre-auth — no
subsystem names, no rollup; **no more than `/api/health`+`/auth/info` already
expose**. D (`§a.3`) returns `{ok,brain}` pre-auth, same posture. Both are clean.

### 2b. Hard-auth vs soft-auth — which is safest/cleanest?

- **A and C (hard-auth, `CurrentUser`)** are the *simplest to reason about*: the
  endpoint cannot leak anything because it 401s without a key. **But** this
  forfeits the one genuine win of the soft-auth design: a **pre-auth / KeyEntry
  server-reachability probe**. With a hard-401 status endpoint, the dot on the
  KeyEntry screen can only ever say "needs-auth," never "server is actually down
  vs your wifi is down" *before* login. That's a real capability the goal asks
  for ("real server reachability vs `navigator.onLine`").

- **B and D (soft-auth)** deliver the pre-auth reachability probe with a tiny,
  test-locked public skeleton. The risk is purely the dual-depth leak surface,
  and B's **two-independent-builders + exact-key-allowlist test** neutralizes it
  structurally (the strongest mitigation of the four). D's is correct but relies
  on a single "unauthed shape == {ok,brain}" test (weaker than B's exact-set
  assertion).

**Recommendation:** the soft-auth single endpoint (B/D shape) is the better
design *because* of the pre-auth probe, **but only with B's two-builder +
exact-allowlist test**. A's "extend `/verify` for the free initial snapshot" is
also worth keeping (zero extra first-paint round-trip) — these compose: extend
`/verify` for the boot snapshot **and** add one soft-auth `/api/system/status`
for the live poll + pre-auth reachability. Do **not** put the live poll on
`/verify` (it runs `owner_name` + `public_key` per call — A correctly notes this).

**One caution on routing:** A puts `/status` under the `/api/auth` prefix as
hard-auth; B/D use a *new* `routers/system_status.py` and explicitly avoid the
owner-gated `/api/system` router (which hard-401s and would break offline
tolerance). **B/D are right to use a new router** — do not hang status off the
existing `system` router (owner-gated) or `chat` router (write-capable). A's
`/api/auth/status` is acceptable but loses the pre-auth probe.

### 2c. CurrentUser / verify_key / _extract_key semantics — all four use them correctly. ✔

`verify_key(_extract_key(req))` never throws (`auth.py:58-71`), so the soft-auth
dependency in B/D is sound. `CurrentUser` (= `require_key`, hard-401 +
throttle) in A/C is correct for a hard-authed endpoint. No misuse.

---

## Area 3 — The search.py degradation fix (Plan C §a0; B §A1n; D mentions)

### 3a. Is `search.py:80-92` really unwrapped? — YES. **C is correct.** ✔

Verified: the two semantic calls at `:81` and `:86` are bare. A `hybrid` or
`semantic` query therefore calls `embeddings.semantic_search → embed →
embed_many → _get_model()`, which **blocks under `_model_lock` while warming**
(could be a multi-hundred-MB first download) or **raises** if fastembed is
missing → uncaught → the whole `/api/search` 500s. There is **no server-side FTS
fallback** for the note/attachment semantic path. C's correction of its own v1
("the server falls back") is right; B's `§A1n` and D's `§e` note correctly
repeat it.

### 3b. Is the proposed try/except → keyword-fallback correct and safe? — Yes, with one refinement. **[LOW]**

C's `§a0` wraps **both** semantic calls in a single `try/except: pass`, so a
warming/missing model degrades to the keyword+entity hits already collected
(`bump`ed into `results`). This mirrors every other branch and is independently
correct — it's a real pre-existing bug fix that should ship regardless of the UI
work.

**Refinement (LOW):** a single `try` around both calls means if
`semantic_search` (notes) raises, `semantic_search_attachments` is skipped too.
That's fine for the warming/unavailable case (both would fail identically), but
for robustness consider matching the existing per-branch granularity (separate
try around each). Also: a bare `except: pass` swallows *all* exceptions including
a genuine query bug — acceptable here (parity with the other branches' bare
`except`), but the convergent design should log at debug level, not silently
discard, to avoid masking a real regression. Not a blocker.

### 3c. Interaction with the embeddings readiness flag — sane. ✔

The fix is **independent** of readiness state (it catches whatever the model
does), so it's correct even if readiness is wrong or absent. The UI gate
(force-keyword while embeddings ≠ ready) and this server fallback are
belt-and-suspenders — both A/B/C/D land on "force keyword in the UI **and**
make the server degrade." Good defense in depth. The one thing to avoid (C calls
this out at `SearchPage` `:79`): don't let the *frontend* fire a semantic request
on every keystroke while warming — force keyword client-side so the server
fallback isn't hammered at keystroke rate. That's frontend, but it's the reason
the server fix alone isn't sufficient.

---

## Area 4 — Scheduler heartbeat

### 4a. Event-loop SQLite write? — Only B proposes the write, and it's correctly off-loop. ✔ / cut-flagged

- **A:** **deferred by design** (`§1d`) — no heartbeat, no new write path.
  Justification (it's a write every 60s; a wedged scheduler isn't a "warn before
  use" synchronous concern) is sound.
- **C, D:** no scheduler heartbeat. (D's registry has no `scheduler` subsystem.)
- **B:** adds `set_meta(get_conn(), "scheduler:last_beat", ...)` **inside
  `asyncio.to_thread`** at the top of each loop iteration (`§A4`), with
  `get_conn()` obtained inside the thread (thread-local correct, `db.py:82-88`).
  **This is correct** — no event-loop SQLite write. B explicitly flags it
  "lowest value of the eleven; ship-or-cut."

### 4b. Is the heartbeat worth it? — Mostly noise. **[LOW] — recommend CUT.**

B's own honest scope nails it: the heartbeat detects a **dead asyncio task**
(rare — the loop swallows per-item errors and never raises out of the `while`),
**not a wedged action** (the realistic failure, but items already run in their
own threads so the loop keeps beating regardless). The image/audio watchdog
(`main.py:93-103`) already recovers the user-visible "stuck pending" case. **The
convergent design should follow A/C/D and omit it.** If a future ops dashboard
wants it, B's off-thread implementation is the correct template.

---

## Area 5 — LLM health

### 5a. Did all four drop the invasive `_last_auth_error` chokepoint? — YES. ✔ ✔

All four converged on **client-side observed-outcome health** and explicitly
**reject** server-side LLM-error mutation:

- **A `§1c`:** no backend LLM state; `configured`/`absent` + `verified:null`;
  validity from the client observed feed.
- **B `§2` (table) + `§C4`:** "DROPPED entirely" — verified the rejection is
  justified (no chokepoint: `complete`/`complete_with_meta`/`complete_with_tools`/
  `stream_turn` across two classes, none catch provider errors). Client-side feed.
- **C `§risk 2`:** `llm.ready` = key present; revoked key caught by observed feed
  + error backstop. No server mutation.
- **D `R2/§b.3`:** explicitly makes the downgrade **client-only** to avoid the
  process-global-shared-mutable-state flapping its own v1 had. Best-articulated.

### 5b. Any backend LLM-health remnant still too invasive? — No. ✔

The only backend LLM signal in any plan is **config-derived presence**
(`settings.has_llm`/`has_anthropic`/`has_xai`, or `llm.has_credentials()`), read
per poll — zero tokens, zero mutation, no model call. This fully satisfies the
"don't burn tokens to prove a key works" constraint. **No invasive remnant.**

One small consistency note: B's snapshot splits `llm` into `anthropic`/`xai`
sub-objects with `kind:"missing"`; A/C use a single `llm` with a `providers`
map. The `providers` map (A/C) is closer to what `ModelPicker` actually consumes
(`llm_keys:{anthropic,xai}` from `/verify`) and folds the duplicate `/verify`
poll cleanly. **Prefer the `providers`-map shape.**

---

## Area 6 — Cost / security / cross-origin / multi-worker

- **Cost:** all four — in-memory reads + at most one `SELECT 1` (A/B; C/D do pure
  flag reads, no DB touch per poll). No model load, no network, no tokens. ✔
  A's `db: SELECT 1` is the only per-poll query and it's the *right* call (it
  catches a locked WAL / read-only mount that a process-only liveness check
  misses). Recommend keeping it — it's trivial and closes a real blind spot.
- **Security:** soft-auth public skeleton is allowlist-tested (B) / single-test
  (D); hard-auth (A/C) leaks nothing by construction. `last_error` truncated
  `[:200]` and post-auth only in all four. No leak into `/api/health` or
  `/auth/info`. ✔ **No HIGH security issue remains.**
- **Cross-origin:** bearer via `Authorization` header, `allow_credentials` off,
  origins `*` (`main.py:232-242`); no cookies on the status path; independent of
  `expose_headers`. All four correct. ✔
- **Multi-worker:** single uvicorn worker confirmed (`server/Dockerfile:45`, no
  `--workers`). All four document the per-process in-memory readiness as a latent
  hazard if `--workers` is added; A/B add LOUD constraint comments. ✔ (anchor
  drift: it's `server/Dockerfile:45`, not `Dockerfile:45` — fix in the hybrid.)

No remaining HIGH in this area.

---

## Best convergent backend design (take X from plan Y)

1. **Readiness state machines — take Plan B's lock discipline + Plan A's
   audio readiness logic.** Both `_set_state` and the readiness snapshot read go
   through **one `threading.Lock`** (B `§A1`/D `§a.1`) so the readiness tuple is
   never torn. Audio keys off `_model_key == want` with A's explicit
   `ready→warming` stale-key downgrade and `model`/`compute_type` echo (A `§1b`).
   Embeddings is the one-shot wrap (any of A/B/C). Wrap sets state **inside
   `_get_model`** (so a cold first request, not just the warmer, records the
   transition) — and *because* of that, **keep the lock** (reject D's
   "delete the lock" reasoning). Do **not** take `_model_lock` in `readiness()`.

2. **Endpoint — take Plan B's soft-auth, two-builder `/api/system/status` on a
   new router**, with the exact-key-allowlist public-skeleton test. Compose with
   **Plan A's free initial snapshot via extending `/verify`** (boot paint, no
   extra round-trip). Live poll hits `/api/system/status`, never `/verify`. New
   dedicated router — not `system` (owner-gated) or `chat` (write-capable).

3. **Capability shape — take Plan A/C's single `llm` object with a `providers`
   map** (`{anthropic,xai}`), `verified:null`, plus per-subsystem `state` +
   truncated `detail`/`last_error` (B/C). Include `db` via `SELECT 1` (A) and
   config-derived `push`/`geocoder` (all).

4. **search.py — take Plan C's `§a0` fix**, refined to per-call try/except with a
   debug log instead of bare `pass`. Ships independently of the UI work.

5. **LLM health — take the (unanimous) client-side observed-outcome model**;
   backend stays config-presence only. No server mutation, no token burn.

6. **Scheduler heartbeat — CUT** (follow A/C/D). If ever needed, use B's
   off-thread `to_thread(set_meta(...))` template.

7. **Public share pre-flight — take Plan C's `llm_ready` boolean on the share
   landing** (`share.py:163/261`, helper already exists at `:197`) — the only
   honest pre-flight for the un-gateable public route, zero new surface.

---

## Top remaining backend MUST-FIXES for the hybrid

1. **[MEDIUM] Lock the readiness tuple.** Adopt B/D's `threading.Lock` around
   both `_set_state` and the readiness read in *both* `embeddings.py` and
   `audio_transcription.py`. A and C currently show unguarded multi-field
   read/write.

2. **[MEDIUM] Reject Plan D's "no lock needed" analysis.** State must be set
   inside `_get_model` (worker/threadpool thread), not only in the
   warmer-after-await, or a request that beats/replaces the warmer leaves
   readiness stuck. Therefore the cross-thread write is real → keep the lock.

3. **[LOW] search.py fix granularity.** Per-call try/except (not one wrapping
   both) + debug log instead of silent `pass`, to avoid masking a real query
   regression while still degrading to keyword on warming/unavailable.

4. **[LOW] Do not take `_model_lock` in `readiness()`.** Confirm the convergent
   design reads `_model`/`_model_key` without acquiring the model lock, so a poll
   can never block behind a model download. Add a one-line comment.

5. **[LOW] Fix the Dockerfile anchor everywhere.** It's `server/Dockerfile:45`,
   not `Dockerfile:45`. Keep the LOUD single-worker constraint comment in both
   readiness modules.

6. **[LOW] Pick one capability shape.** Use the single-`llm`-with-`providers`-map
   (A/C) over B's split `anthropic`/`xai` sub-objects — it matches what
   `ModelPicker` consumes and folds its duplicate `/verify` poll.

No HIGH backend issues remain; the design is shippable once the lock discipline
(must-fix 1–2) is settled.
