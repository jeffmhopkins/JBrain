# Red Team 3 — Adversarial review of the HYBRID plan (round 3)

**Reviewer:** Red Team (round 3) · **Date:** 2026-06-08 · **Target:**
`50-hybrid-v1.md` · **Scope:** backend + frontend as ONE plan, verified against the
live tree (`server/app/...`, `web/src/...`). Verification legend: ✅ verified ·
⚠️ caveat · ❌ wrong-against-code.

**Verdict up front:** the hybrid is *close* and the architecture is sound. The two
HIGH issues both red teams flagged (search.py bug, offline-auth safety) are
correctly carried. But this is **NOT yet ready to be the final plan.** There is one
**hard correctness bug in shippable code** (`clock.iso_now()` does not exist), a
**real semantic conflict between the two LLM-readiness definitions** (status doc vs
share landing), an **under-specified store reconciliation** (the boot `/verify`
snapshot vs the polled snapshot are described as one store but never reconciled, and
the manual `connect()` path is missed), and a **stall-vs-user-abort ambiguity** in
the observed feed that will emit false `stall` reports. None are architecture-level;
all are concrete must-fixes for the iteration pass.

---

## What I re-verified as CORRECT in the hybrid (so the iteration doesn't re-litigate)

- **search.py:80-92 bug** ✅ exact. The two semantic calls (`semantic_search`
  `:81`, `semantic_search_attachments` `:86`) are bare; every other branch is
  wrapped (`:37-48,50-64,69-78,94-103`). The proposed per-call try/except + debug
  log is correct and behavior-preserving. **This is the load-bearing fix and it is
  right.**
- **Offline-auth invariant** ✅. `App.tsx:106` is exactly
  `.catch((e)=>{ if(e?.status===401) clearAccessKey(); else setAuthed(true); })`.
  `getStatus()` is non-throwing and never calls `clearAccessKey`; the `api()` diff
  keeps `if (res.status === 401) throw new ApiError(...,401)` intact. Safe by
  construction. ✅
- **Lock discipline** ✅. `_set_state` inside `_get_model` runs on the `to_thread`
  worker (`main.py:180,211`) and on the route threadpool — a real cross-thread
  write; a single `threading.Lock` around both `_set_state` and the snapshot read
  is correct. `import threading` already present in both modules
  (`embeddings.py:7`, `audio_transcription.py:21`). Not taking `_model_lock` in
  `readiness()` is the right call.
- **Audio reload keying** ✅. `_get_model` reloads on `_model is None or
  _model_key != want` (`:98`); `_model_key` set only after success (`:109`); a
  failed reload leaves the old key ≠ want → reads `warming`, never false `ready`.
  Correct.
- **Anchors** mostly exact: `api.ts` `authHeaders:34`, `api()` `:40-57` (no
  try/catch), `streamChat` fetch `:735` outside read-loop try `:755`, `streamSSE`
  fetch `:806` outside try `:818`, `STALL_MS=90000` `:752/:815`; `App.tsx` share
  `:124`, `path="*"` `:125`, KeyEntry `:127`, Shell `:129`, OwnerChat `:138`;
  `Shell.tsx` ReviewBell `:243`, brand `:240`, version banner `:258-259`, offline
  banner `:261`, resume handlers `:49-51`; `SearchPage` MODES `:36`, default `:44`,
  query `:77`, swallowed catch `:79`, mode seed `:43-44`; `Attachments` `hasLlm:38`,
  Transcribe `:284`, Analyze `:290`; `ModelPicker` `/verify` re-fetch `:37,48-52,61`;
  `SharePage` getShare `:52`, guided/research/chat `:66-78`. `share.py`
  `share_read:108`, `_guided_landing:163`, `_resolve_guided:187`, `llm_ready:197`,
  `_research_landing:261` (in **`routers/share.py`** — §3.6 header is right; the
  provenance table's bare `share.py:197` is loose but resolves correctly).
- **CORS/cross-origin** ✅ `main.py:232-242` `allow_credentials` off, origins `*`,
  bearer-only, `get_conn()` thread-local (`db.py:82`) so a sync status route on the
  threadpool is safe. **Dockerfile** ✅ `server/Dockerfile:45` CMD, no `--workers`.
- **Subsystem sources** ✅ `push.public_key():67`, `geocode.enabled():38`,
  `llm.has_credentials():506`, config `has_llm/has_anthropic/has_xai` properties
  (`config.py:71,75,80`).

---

## HIGH findings

### H1 — `clock.iso_now()` does not exist. The public skeleton won't import. ❌

§3.3 `_public_skeleton()` returns `{"ok":True,"brain":...,"ts": clock.iso_now()}`
and the router imports `from ..services import clock`. **`clock.py` has no
`iso_now()`** — only `app_tz_name()` (`:41`), `now_utc()` (`:51`), `now_local()`
(`:55`), `now_prompt()` (`:72`). This is **shippable code in the plan that raises
`AttributeError` on every `/api/system/status` call AND on every `/verify`** (which
folds in the same builder). The plan hedges with "or `datetime.now(UTC).isoformat()`"
but the primary, copy-paste path is wrong, and the public-skeleton **exact-allowlist
test** (§7) is written against keys `{ok,brain,ts}` — if an implementer takes the
literal `clock.iso_now()` it 500s before the test can even assert the allowlist.
**MUST-FIX:** specify the real call — `clock.now_utc().isoformat()` (or add a real
`iso_now()` helper to `clock.py` and cite its anchor). Decide and pin one.

### H2 — Two different definitions of "LLM ready" that will visibly disagree. ⚠️→HIGH

- The **status capabilities doc** (§3.2) computes `llm.state` from
  `s.has_llm`/`s.has_anthropic`/`s.has_xai` — **config properties** (key strings
  present).
- The **share landing** (§3.6) computes `llm_ready` from `llm.has_credentials()`
  (`llm.py:506` → `get_provider().has_credentials()`) — **the active provider's**
  credential check.

These are not the same predicate. With `LLM_PROVIDER=xai` and only `LLM_API_KEY`
(Claude) set, `has_anthropic` is true → status dot shows LLM **configured/green**,
while `has_credentials()` for the xai provider is false → share landing shows
**"temporarily unavailable"** and owner chat 404s. The owner sees a green dot but a
dead assistant. The two surfaces the plan introduces contradict each other on a
real config. **MUST-FIX:** single-source the predicate. Either make
`capabilities().llm.state` derive from `llm.has_credentials()` (the predicate that
actually gates feature execution everywhere else), or document why presence is the
intended signal and reconcile the dot with the share/chat reality. The `providers`
map can stay for the ModelPicker per-provider warning, but the top-level
`configured/absent` must match what the server will actually run.

---

## MEDIUM findings

### M1 — The "one store" claim is not actually reconciled; `connect()` is missed. ⚠️

§1 design and §4.5 say the boot `/verify` capabilities become "the store's initial
snapshot (free first paint)" and the live poll hits `/api/system/status`. But:

1. **`/verify` and `/api/system/status` return different shapes.** `/verify` is
   extended with `capabilities` (§3.4) but ALSO keeps `has_llm`, `llm_keys`,
   `vapid_public_key`, etc. The status doc nests everything under `capabilities`
   plus `ok/brain/ts/version`. The store ingests both via `applySnapshot(data)` —
   but the plan never says how `applySnapshot` normalizes the two different
   envelopes into one `Capabilities`. Two callers, one un-specified normalizer =
   drift waiting to happen. **Pin the adapter** (verify → `data.capabilities`;
   status → `data.capabilities`; both feed the same setter) explicitly.
2. **Only `App.tsx:102` (the boot effect) is cited.** The **manual login path
   `connect()` `:77-88`** ALSO calls `get("/api/auth/verify")` (`:81`) and is where
   a first-time user lands. If only `:102` seeds the store, a fresh login shows
   `unknown` until the first poll instead of the promised "free first paint."
   **MUST-FIX:** seed the store from BOTH `:81` and `:102`, or (cleaner) have the
   poller's `refreshNow()` fire on `connect()` (the plan mentions this in §4.4 —
   make it the single seeding mechanism and drop the "store the `/verify` caps"
   path to avoid the dual-envelope normalizer entirely).

### M2 — Stall watchdog cannot distinguish user-abort from stall → false `stall` reports. ⚠️

§4.3 wants `streamChat`/`streamSSE` to `report({kind:"stall"})` "when the stall
watchdog fires (`ctrl.abort()` at `STALL_MS`)." But in the live code the only
signal the read loop sees is `reader.read()` throwing, caught by `catch { break; }`
(`:759`, `:821`) — **the same path taken when the USER aborts** (leaving chat /
starting a new turn, via the `signal` wired at `:731-733`). The plan's `neterr`
branch correctly guards `if (!ctrl.signal.aborted)`, but a **stall** also sets
`ctrl.signal.aborted`, so you cannot tell "server went silent 90s" from "user
navigated away" at the catch site. **MUST-FIX:** set an explicit
`let stalled = false;` flag inside the `arm()` timeout callback (`() => { stalled =
true; ctrl.abort(); }`) and report `stall` only when `stalled`, else treat an
aborted read as a benign user cancel (no report). Without this, every normal "leave
the chat" emits a stall → spurious amber/red and a spurious out-of-band poll.

### M3 — `needs-auth` detection is mostly dead on a soft-auth route — and the plan half-admits it. ⚠️

§4.1 itself notes that on the soft-auth `/status` an invalid/rotated bearer returns
the **200 skeleton**, so `getStatus()` literally **never returns
`reason:"needs-auth"`** in normal operation (the 401 branch only fires if the route
is mis-deployed under a hard-auth dependency). The plan then proposes a second
path: "got skeleton but we have a stored key → `needs-auth` in the store (§4.2)."
**But §4.2's reconciliation rules (1-4) never mention this mapping.** So the one
genuinely useful `needs-auth` UX (rotated key → amber "Re-authenticate", FE
MUST-FIX 10) is asserted in two places and **implemented in neither**. Either:
(a) detect it concretely — `getStatus` returns `{ok:true,data}` where
`data.capabilities === undefined` (skeleton) **and** `getAccessKey()` is non-null →
store sets `server:"needs-auth"`; add this as reconciliation rule 0 — or (b) drop
the `needs-auth` state and accept that a rotated key shows green-skeleton until the
next real `api()` call 401s (which still doesn't log out). Pick one and write it
into §4.2. As written it's a contract with no implementation.

### M4 — `mode==="semantic"` will look empty while embeddings warm, even with the server fix. ⚠️

The §3.5 server fix degrades *hybrid* to keyword gracefully (keyword hits already
`bump`ed). But a **pure `semantic`** query collects ONLY semantic results; with the
try/except swallowing both calls, `results` is empty → `ranked` is empty →
`search()` returns `[]`. `search.py:118` then sorts an empty list by distance. So a
user on `mode=semantic` during warmup gets "No results" (`SearchPage:101`), not a
degraded list. The plan's client gate ("force `hybrid` while embeddings ≠ ready,"
§4.7 search-semantic row) is what actually saves this — **but that gate is therefore
NOT optional defense-in-depth; it is required for `semantic` mode to be non-broken
during warmup.** Make §4.7's "force hybrid" a hard dependency of Phase 7, and note
that the URL can seed `mode=semantic` (`:43-44`) so the force must run on mount, not
just on click.

### M5 — Observed feed instruments only `streamChat`/`streamSSE`/`api()`, but rebuild/research SSE LLM failures won't self-heal LLM state cleanly. ⚠️ (LOW-MED)

§4.3 maps a chat `{type:"error"}` event → `llm-fail` and a clean `done` → `llm-ok`.
But `streamSSE` (rebuild) also yields `{type:"error", message}` (`api.ts:797`) and a
`done` — the plan does NOT say rebuild errors feed `llm-fail`/`llm-ok`. A revoked
key surfaces as `degraded` only via chat traffic, not via a rebuild/research run.
Minor (chat is the common path), but the plan claims "downgrade LLM health from real
request outcomes" generally — either wire the rebuild SSE error/done too, or scope
the claim to chat. Don't over-claim.

---

## LOW findings

- **L1 — Two routers share `prefix="/api/system"`.** `system.py:27` (owner-gated,
  hard-401) and the new `system_status.py` (soft-auth) both mount `/api/system`.
  No path collision (`/status` is new; verified `system.py` has no `/status`
  route), and FastAPI allows it, but it's a **foot-gun**: a future reader assumes
  everything under `/api/system` is owner-gated. The plan even warns "NOT the
  owner-gated `/api/system` router" while then *using the same prefix string*.
  Consider `/api/system/status` justified, but add a one-line comment in
  `system_status.py` that it deliberately shares the prefix with a *different* auth
  posture, and confirm registration order in the `main.py:244` loop doesn't matter
  (it doesn't — distinct paths).
- **L2 — `auth_router.py` import surface.** §3.4 says "keep every legacy field";
  verified `/verify` already returns all of them (`:33-38`), and `system_status` is
  importable from `auth_router`. Fine — but note `auth_router.py` currently imports
  `clock, push, people` lazily *inside* `verify()` (`:27`); adding
  `system_status.capabilities()` (which itself imports `embeddings,
  audio_transcription, push, geocode, db`) inside `verify()` is fine but means the
  free boot snapshot now also runs `SELECT 1` on every `/verify`. Acceptable
  (cheap), but state it — `/verify` is no longer free of DB-touch.
- **L3 — `db: SELECT 1` per poll on the same thread-local conn as writes.** Benign,
  but on a single-worker server the status route's threadpool thread gets its own
  `get_conn()` (thread-local) — fine. Just confirm the assembler uses `get_conn()`
  (read) not a write conn; the plan does (`db.py:82`). ✅ no action.
- **L4 — KeyEntry reachability line is asserted but not anchored.** §4.5 says "a
  minimal one-line reachability banner renders on KeyEntry" — `KeyEntry` was not in
  the verify list and the plan gives no anchor. Confirm `KeyEntry.tsx` has a slot
  and that it can read the store pre-auth (the provider mounts above the gate, so
  it can). Low risk, but un-anchored.
- **L5 — Provenance table file-path sloppiness.** Several rows cite bare `share.py`
  / `config.py` without the `routers/` vs `services/` qualifier. The functions live
  in **`routers/share.py`**, not `services/share.py` (which has none of them). §3.6
  gets it right; the table should too, to avoid an implementer editing the wrong
  file.

---

## Goal check — does the hybrid deliver the owner's two asks?

**(a) Real-time server AND API health:** ⚠️ mostly. Server reachability (3-axis) is
genuinely delivered, including pre-auth on KeyEntry. "API/LLM health" is
presence + observed-outcome — honest and correctly scoped to avoid token burn.
**But** H2 means the headline "LLM" signal can be *wrong* (green while dead) on a
provider-mismatch config, which undercuts the "API health" claim specifically.

**(b) Warn-before-use for EVERY unrunnable service:** ⚠️ with gaps.
- Embeddings/transcription/LLM gating: covered by §4.7 (Plan C inventory, verified
  accurate on spot-check).
- **Public share route:** covered server-side via `llm_ready` landing flag —
  **the only honest option**, verified `_resolve_guided:192`/`_resolve_research:292`
  already 404 on `not llm_ready()`. ✅ But subject to H2 (the landing uses the
  *correct* `has_credentials()` predicate; the dot uses the *wrong* one — so the
  share route is right and the dot is the liar).
- **Entry/capture, keyword search, E2EE chat:** correctly left ungated (no AI
  dependency). ✅
- **Falls-through:** the `semantic`-mode empty-results case (M4) is a "warns by
  showing nothing," not a real warning, until the force-hybrid gate ships. And
  rebuild/research SSE LLM failure (M5) isn't fed to the degraded signal.

---

## Residual HIGH/MEDIUM risk re-scrutiny

- **Public skeleton allowlist (security):** the design (two independent builders,
  exact-key test) is sound — *if* H1 is fixed so the builder doesn't throw before
  the test runs. The skeleton leaks nothing beyond `/auth/info`+`/health`. ✅
- **Multi-worker:** correctly handled with LOUD comments; `server/Dockerfile:45`
  anchor now correct. ✅
- **Toast storms:** SearchPage `:79` exclusion kept ✅; "promote silent catches only
  when server==='ok'" rule is good. One residual: M2's false-`stall` would also
  drive a toast/poll — fixing M2 closes it.
- **Re-render performance:** `useSyncExternalStore` singleton is the right choice ✅.
- **Cost:** zero token burn confirmed ✅.

---

## Scope / over-build check

Mostly disciplined. Two things to consider cutting/deferring:
- **`needs-auth` 4th server state** (M3): if you can't implement the skeleton-vs-
  stored-key detection cleanly, **cut it** — it's the least load-bearing feature and
  is currently un-implemented. A rotated key showing green-then-401-on-next-call is
  acceptable for a single-user app.
- **`Pydantic CapState`/`Capability` models (§3.9):** marked "optional"; for a
  single-user self-hosted app with a hand-maintained TS `Capabilities` interface,
  this is duplicate contract surface. Defer unless the OpenAPI schema is actually
  consumed.

Everything else (3-axis dot, observed feed, toast, gating inventory) is justified by
the explicit owner goal.

---

## VERDICT: NOT ready as the final plan. Must-fix before locking.

The architecture is right and round-2's findings are correctly carried, but there
are concrete bugs and unimplemented contracts that would ship broken.

### MUST-FIX (blockers)
1. **[H1] Replace `clock.iso_now()`** with `clock.now_utc().isoformat()` (or add the
   helper to `clock.py` and anchor it). It appears in both `/status` and the folded
   `/verify` path; as written both 500.
2. **[H2] Single-source the LLM-ready predicate.** Make the status doc's `llm.state`
   agree with `llm.has_credentials()` (what the share landing and all feature gates
   use), or explicitly justify presence-vs-credentials and reconcile the dot with
   the share/chat 404 reality.
3. **[M1] Pin the store-seeding path and envelope adapter.** Seed from `connect()`
   (`:81`) AND boot (`:102`) — preferably via a single `refreshNow()` on connect —
   and specify how `/verify` vs `/status` envelopes normalize into one
   `Capabilities`.
4. **[M2] Distinguish stall from user-abort** in `streamChat`/`streamSSE` with an
   explicit `stalled` flag; only `report({kind:"stall"})` on a real watchdog fire.
5. **[M3] Implement or cut `needs-auth`.** If kept, add the concrete
   "skeleton-returned-but-stored-key-present → needs-auth" rule to §4.2's
   reconciliation list; if not, remove the 4th state from §4.1/§4.6.
6. **[M4] Promote the SearchPage "force hybrid while embeddings ≠ ready" gate to a
   hard requirement** (run on mount, since the URL can seed `mode=semantic`) —
   pure `semantic` returns `[]` during warmup even with the server fix.

### SHOULD-FIX (iteration pass)
7. **[M5]** Wire (or explicitly de-scope) rebuild/research SSE `error`/`done` into
   the LLM observed-health signal.
8. **[L1]** Comment the deliberate shared `/api/system` prefix with split auth
   posture.
9. **[L2]** Note that the folded `/verify` now does a `SELECT 1` (no longer
   DB-touch-free).
10. **[L4/L5]** Anchor the KeyEntry reachability slot; fix `routers/` vs `services/`
    path qualifiers in the provenance table.

No HIGH security regressions; the offline-auth invariant, CORS posture, lock
discipline, search.py fix, and single-worker handling are all correct. Fix the six
blockers and this becomes shippable.
