# Plan D v2 — Observed Health First (poll-driven), SSE shelved

**Author:** Architect D (original) · **Repo:** /home/user/JBrain · **Date:** 2026-06-07
**Supersedes:** `13-plan-D-realtime-stream.md` (v1). Responds to `20-redteam1-D.md`.

## Thesis (changed)

v1's headline was an SSE status stream. The red team is right: **for a single
self-hosted user on the canonical Caddy/HTTP-2 deploy, that stream earns almost
none of its complexity.** The one genuinely great idea in v1 — *observed health
from real traffic* — needs **zero** transport and works perfectly with a poll.

So v2 inverts the plan: **the heart is observed health** (instrumenting the
central `api()` wrapper + `streamChat`/`streamSSE` so the indicator reflects what
is *actually* happening to real requests, between polls, at zero token cost),
backed by a cheap snapshot endpoint + adaptive poll for declared subsystem
readiness. **SSE is dropped from the primary design and documented as a
deferred, gated-behind-multi-user enhancement** — kept on the shelf, not built.

---

## Changes from v1 / red-team responses

| # | Red-team point | v2 response |
|---|---|---|
| **1** | **6-connection cap is obsolete under Caddy HTTP/2** (§1f) — multiplexed streams, not TCP connections; the `api.ts:728-730` symptom was un-aborted HTTP/1.1/dev-proxy POSTs, not proof the H2 prod path has the problem. | **Conceded in full.** The cap concern was v1's self-declared "biggest weakness" and it was overstated. On the primary deploy (Caddy auto-HTTPS → HTTP/2, `Caddyfile.template:8`) chat-stream + status-stream would be 2 multiplexed streams on **one** connection, not "2 of 6." This removes a load-bearing justification for SSE and is part of why SSE is now demoted (see §SSE decision). |
| **2** | **Is the SSE stream worth it for this single user? No.** The only pushed transitions are embeddings/audio flipping ready ~5s after boot, watched by nobody (§2). | **Agreed → SSE DROPPED as primary.** v2 leads with snapshot + adaptive poll (5s while warming / 20s steady). The boot transition is caught within one tick of the 5s warming cadence. See **§SSE keep/drop decision** for the explicit recommendation and the narrow condition under which SSE would ever be added. |
| **3** | **Hanging health off `/api/chat/*`** — the highest-privilege, write-capable router whose invariant is "a stale client must NEVER silently gain WRITE tools" (`chat.py:50-56`) — to dodge a 2-line Caddyfile edit is a security/maintenance smell (§1a/§1b). | **Conceded.** v2 places health on a **new dedicated soft-auth router `routers/system_status.py`** (`GET /api/system/status`), NOT on `chat.py` and NOT on the owner-gated `/api/system` router (which hard-401s, breaking the public reachability probe). Poll needs no Caddy change at all. *If* SSE is ever added, it gets a proper 2-line `@sse2 path /api/system/status/stream` Caddy block (shown in §SSE decision), not a router hack. |
| **4** | **api.ts anchor is wrong:** there is **no** outer try/catch around `streamChat`'s initial fetch (`api.ts:735-740`); `api()` itself (`api.ts:40-57`) also has none — network errors throw raw. You must *add* try/catch, not instrument an existing catch. (§1d) | **Corrected and verified.** I re-read the code: `api()` (40-57) has no try/catch; `streamChat`'s `try` (755) wraps only the read loop, with the `fetch` at 735-740 *outside* it; `streamSSE` (821) is a per-read `catch{break}`. v2 §b.2 **adds** try/catch in each of the three places and shows the exact diffs. v1's "instrument the catch" framing is withdrawn. |
| **5** | **`bind_loop`/`call_soon_threadsafe` registry machinery is probably dead weight** — warmer `set_state` calls land on the event loop (`await asyncio.to_thread(...)` returns to the loop), so the cross-thread path isn't hit (§1e). | **Verified and simplified.** `_warm_embeddings`/`_warm_audio` (`main.py:177-215`) do `await asyncio.to_thread(_get_model)`; the `set_state` after the await runs **on the loop**. The observed-LLM downgrade also runs in async context. **No worker-thread `set_state` call site exists.** v2 drops `bind_loop`/`_loop`/`call_soon_threadsafe` entirely; a plain dict + `threading.Lock` (defensive, cheap) is the whole registry. (With SSE gone there's no `asyncio.Queue` to protect anyway.) |
| **R2/§9** | Server-side observed-LLM downgrade is **process-global shared mutable state**: one tab's transient 5xx flips `llm` to "errored" for every session with no per-cause TTL. | **Made client-side only.** The observed-LLM downgrade lives in the **client** health store (transient, self-healing, per-tab). The server snapshot reports `llm` as config-derived `configured`/`absent` only — never mutated by a request outcome. No shared global flapping. (Optional strictly-TTL'd server-side last-error is listed as a *future* nicety, not built.) |
| **A-1.1** (stolen from red-team A) | Audio readiness must key off `_model_key`, not a one-shot flag — `_get_model()` **reloads** when the Settings-GUI model/compute_type changes (`audio_transcription.py:97-109`). | **Adopted.** v2's audio readiness tracks the `(model, compute_type)` the cached model was loaded with, so an in-Settings model change flips state back to `warming` instead of lying `ready`. Embeddings never reload, so its flag is a simple one-shot. |

---

## Verified ground truth (re-checked against code, 2026-06-07)

- `GET /api/health` (`main.py:254-256`) → `{ok, brain}`, public liveness. ✔
- `/api/auth/verify` (`auth_router.py:22-38`) is the de-facto manifest. ✔
- Embeddings `_get_model` (`embeddings.py:20-30`) lazy under `_model_lock`; **no reload**, no readiness flag. ✔
- Audio `_get_model` (`audio_transcription.py:93-110`) lazy; **reloads** when `(audio_model(), audio_compute_type())` changes (`:97-100`, `_model_key` at `:39`); raises `TranscriptionUnavailable` on missing dep (`:103-107`). No readiness flag. ✔
- Warmers `_warm_embeddings` (`main.py:177-202`) / `_warm_audio` (`main.py:208-215`): `asyncio.create_task`, each `await asyncio.to_thread(_get_model)`, errors swallowed. **The `set_state` after the await is on the event loop.** ✔ (kills §a.1's cross-thread machinery)
- `/api/system` router is **owner-gated, hard-401** (`routers/system.py:27` `dependencies=[CurrentUser]`). A health probe here would break the offline-tolerant / pre-auth-reachability contract → needs a **separate soft-auth router**. ✔
- Caddy: only `/api/chat/*` is unbuffered (`Caddyfile.template:33-37`). Caddy auto-HTTPS ⇒ **HTTP/2** (`:1-3, :8`). The template is rendered by `install.sh` and re-rendered on update — so a template edit ships to every operator. ✔
- `api()` (`api.ts:40-57`): **no try/catch**; network errors throw raw; 401 throws `ApiError(401)`; non-OK parses `detail`. ✔
- `streamChat` (`api.ts:719-777`): `fetch` at **735-740 is OUTSIDE** the `try` (755); the `try` wraps only the read loop; stall watchdog `STALL_MS = 90000` (`:752`). ✔
- `streamSSE` (`api.ts:803-838`): per-read `catch{break}` at **821**; `STALL_MS = 90000` (`:815`). ✔
- `authHeaders` (`api.ts:34`) is **module-private** — NOT exported. The poller must use the exported `get()`/`api()`, or we export `authHeaders`. v2 uses `api()` with care (see §c). ✔
- Offline-tolerant auth: only a real 401 clears the key (`App.tsx:106`). ✔
- CORS: `allow_credentials` OFF, origins default `*`, bearer-only (`main.py:232-242`). ✔
- Banners at `Shell.tsx:258-261`; status area beside `ReviewBell` (`Shell.tsx:243`). `useOnline` is `navigator.onLine`-only (`hooks.ts:264-277`). ✔
- Existing gate: only `Attachments.tsx:38` via `hasLlm`. `AuthState` thin (`App.tsx:113+`). ✔

---

## SSE keep/drop decision (explicit recommendation)

**Recommendation: DROP SSE from this plan. Ship snapshot + adaptive poll +
observed-health.** Document SSE as a future option, do not build it now.

**Why (intellectually honest):**

1. **The transitions don't justify push.** The entire universe of server-pushed
   state changes is: `embeddings` and `audio` going `warming→ready|unavailable`,
   **once, ~5 seconds after a server restart** — a moment one user almost never
   watches. `llm/db/push/geocoder` are config constants. Adaptive poll (5s while
   warming) catches the only live transition within one tick. The push-vs-poll
   latency win is *seconds, once per restart, observed by nobody.*
2. **The cap argument that propped up "real-time matters" is gone (§1f).** Under
   HTTP/2 the connection-contention story I leaned on doesn't apply — but the
   flip side is that "real-time" had no strong demand to begin with for one user.
3. **SSE drags in a tail of complexity that exists only to serve SSE:** a
   long-lived socket, a subscriber registry + queues, a keepalive/watchdog
   timing regime *tighter* than chat's 90s (a new regime, not "reuse the
   transport" as v1 claimed), a proxy-buffering failure mode (nginx default-
   buffers even if Caddy doesn't), AND a poll fallback whose only job is to
   detect that failure mode. Deleting SSE deletes that entire stack.
4. **The best idea never needed it.** Observed-health (the heart of v2) is pure
   client instrumentation and is *strictly more truthful* than any poll-or-push
   declared-readiness scheme, because it reflects real request outcomes between
   ticks.

**The one condition that would flip this:** the deployment goes **multi-user or
adds an ops/status dashboard** where a human watches another box's subsystems
come up, or where sub-5s convergence across many viewers matters. Then, and only
then, add SSE as a **layer on top of the unchanged poll-fed store** — the store,
indicator, gating, and observed feed below are all transport-agnostic, so SSE
becomes additive, not a rewrite.

**If SSE is ever added, the clean shape (not the v1 hack):**

```
# Caddyfile.template — 2-line addition, ships via update.sh re-render
@sse2 path /api/system/status/stream
reverse_proxy @sse2 api:8000 { flush_interval -1 }
```
…and a `GET /api/system/status/stream` on the **same dedicated
`system_status` router** (soft-auth, fetch+ReadableStream reader, bearer — not
`EventSource`, not `credentials:"include"`), replaying `snapshot()` first then
emitting `set_state` events. The client store gains one input (`source:"server"`
events) and keeps poll as the fallback. **Not built in this plan.**

---

## (a) Backend: readiness registry + snapshot endpoint (no SSE, no event loop machinery)

### a.1 Registry — `server/app/services/health.py` (new, ~40 lines)

Plain dict + a `threading.Lock`. **No `bind_loop`, no `_loop`, no
`call_soon_threadsafe`, no `asyncio.Queue`, no subscribers** (§5).

```python
import threading, time
from typing import Literal

State = Literal["unknown", "warming", "ready", "degraded", "unavailable"]

_lock = threading.Lock()
_subs: dict[str, dict] = {
    n: {"state": "unknown", "detail": None, "changed_at": time.time()}
    for n in ("embeddings", "audio", "llm", "db", "push", "geocoder")
}

def set_state(name: str, state: State, detail: str | None = None) -> None:
    """Idempotent; only stamps changed_at on a real transition. Lock-guarded so
    it's safe from any caller, though all current callers are on the event loop."""
    with _lock:
        s = _subs[name]
        if s["state"] == state and s["detail"] == detail:
            return
        s.update(state=state, detail=detail, changed_at=time.time())

def snapshot() -> dict:
    with _lock:
        return {"subsystems": {k: dict(v) for k, v in _subs.items()}}
```

State machines: `embeddings` `unknown→warming→ready|unavailable`; `audio` same,
but **keyed on `(model, compute_type)`** so a Settings-GUI model change re-enters
`warming` (§A-1.1); `llm` config-derived at boot (`ready` if `settings.has_llm`
else `unavailable`) — **never probed, never mutated by request outcomes**
(downgrades are client-side, R2); `db` `ready` after `init_db`; `push`/`geocoder`
from config. `unavailable` for audio is *expected* on minimal installs → UX
"feature off," not an error.

### a.2 Wire warmers — `server/app/main.py`

At lifespan start seed config states (`llm`/`push`/`geocoder`/`db`). In
`_warm_embeddings` (`:177`): `set_state("embeddings","warming")` before the
`to_thread`, `"ready"` after it returns (on the loop — no thread-safety needed),
`"unavailable"` in the `except` (`:200`). In `_warm_audio` (`:208`): same, but
the readiness wrapper records the loaded `(model, compute_type)` so a later
reload re-arms `warming`.

Minimal in-service flags do the keying so the registry stays dumb. In
`audio_transcription.py`, add a tiny `readiness()` that compares the live
`(audio_model(), audio_compute_type())` against the `_model_key` the cached model
was built with (`:39`): `ready` only if `_model is not None and _model_key ==
want`; else `warming`/`unavailable`. The warmer calls `set_state` from that.

### a.3 Snapshot endpoint — `server/app/routers/system_status.py` (new, soft-auth)

**Not on `chat.py`** (§3). **Not on owner-gated `/api/system`** (hard-401 would
break the pre-auth reachability probe and offline tolerance). A new lightweight
router with **soft auth** (a dependency that returns whether the bearer is valid
but never raises), mirroring Plan B's `optional_key` approach:

```python
from fastapi import APIRouter, Request
from ..auth import verify_key, _extract_key       # reuse existing primitives
from ..config import get_settings
from ..services import health

router = APIRouter(prefix="/api/system", tags=["status"])   # NO router-level CurrentUser

def _authed(req: Request) -> bool:
    try: return verify_key(_extract_key(req))
    except Exception: return False

@router.get("/status")
def status(req: Request):
    s = get_settings()
    public = {"ok": True, "brain": s.brain_name}
    if not _authed(req):
        return public                              # pre-auth: liveness only, no cap names
    return {**public, "version": APP_VERSION, **health.snapshot()}
```

- **Public, unauthed →** `{ok, brain}` only: this is the **server-reachability
  probe** the dot needs, callable on KeyEntry and offline-tolerant (never a 401).
- **Authed →** full `snapshot()`. Key-gated detail, no pre-auth capability leak
  (§8 security). Cheap: in-memory reads, no DB write, no model touch, no network.
- Register in the `main.py:244` router loop. **Zero Caddy change** (it's a normal
  buffered JSON GET; buffering is irrelevant without a stream).

---

## (b) Client: observed health from real traffic — THE HEART

This is v1's best idea, now the centerpiece and **fully decoupled from any
stream**. It works identically whether state arrives by poll or (future) push.

### b.1 Health store + bus — `web/src/health.ts` (new)

Dependency-free pub/sub singleton (mirrors the existing `TTS_ON_EVENT` pattern),
exposed to React via `useHealth()` built on `useSyncExternalStore`.

```ts
export type SubState = "unknown"|"warming"|"ready"|"degraded"|"unavailable";
export interface HealthModel {
  link: "online" | "server-unreachable" | "browser-offline";
  server: { state: "ok"|"degraded"|"unreachable"|"unknown"; lastSeen: number };
  subsystems: Record<string, { state: SubState; detail?: string|null;
                               source: "declared"|"observed" }>;
  observed: { last5xxAt?: number; lastNetErrAt?: number; lastStallAt?: number };
}
```

Inputs: (1) **declared** state from the poll snapshot →
`subsystems[name]={...,source:"declared"}`, stamps `server.lastSeen`; (2)
**observed** outcomes from real traffic (b.2). API: `subscribe(cb)`,
`getModel()`, `report(event)`, `applySnapshot(snap)`.

### b.2 Feed `api()` / `streamChat` / `streamSSE` outcomes (corrected anchors — we ADD try/catch)

**`api()` (`api.ts:40-57`) — wrap the bare `fetch` (there is no existing
try/catch):**

```ts
export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(u(path), { ...opts, headers: authHeaders(opts.headers) });
  } catch (e) {
    health.report({ kind: "neterr" });            // network failure → server suspect
    throw e;                                       // behavior UNCHANGED
  }
  health.report({ kind: "http", status: res.status });
  if (res.status === 401) throw new ApiError("Not authenticated", 401);  // still throws → App.tsx:106 untouched
  if (!res.ok) { /* ...existing detail parsing, unchanged... */ }
  if (res.status === 204) return undefined as T;
  return res.json();
}
```

**`streamChat` (`api.ts:735-740`) — the fetch is OUTSIDE the read-loop `try`; add one around it:**

```ts
let res: Response;
try {
  res = await fetch(u(`/api/chat/conversations/${conversationId}/message`),
    { method: "POST", headers: authHeaders(), body: JSON.stringify(body), signal: ctrl.signal });
} catch (e) {
  if (!ctrl.signal.aborted) health.report({ kind: "neterr" });   // ignore user-abort
  throw e;
}
health.report({ kind: "http", status: res.status });
if (!res.body) throw new ApiError("No response stream", 500);
```

Also: when the stall watchdog fires (`ctrl.abort()` at `STALL_MS`, `:753`) →
`health.report({ kind: "stall" })` and trigger an immediate snapshot re-poll.

**`streamSSE` (`api.ts:806-810`) — same treatment** around its `fetch` (`:806`).

Mapping in the store:
- `http status>=500` → `last5xxAt=now`, `server.state="degraded"`.
- `http status<500` (incl. 401/4xx) → server is answering → `server.lastSeen=now`.
- `neterr` → `lastNetErrAt=now`. `stall` → `lastStallAt=now`, server suspect.
- **401 still throws unchanged** → `App.tsx:106` logout path untouched (R5).

### b.3 Client-side observed-LLM downgrade (R2 fix: client-only)

A `streamChat` `{type:"error"}` event or a chat POST 5xx →
`subsystems.llm={state:"degraded",source:"observed"}` **in this tab's store
only**. Self-heals: the next successful turn, or the next poll's declared
`llm:ready`, restores it. **No server-global mutation, no cross-tab flapping.**

### b.4 Reconciliation (declared vs observed vs browser)

1. `navigator.onLine===false` → `link:"browser-offline"` (dominates; don't poll).
2. else recent `neterr`/`stall` **AND** no server byte within ~8s →
   `link:"server-unreachable"` — the key new signal ("my server is down" vs "my
   Wi-Fi is down"), sharper than a failed poll because traffic witnesses it first.
3. else `link:"online"`; `server.state` = worst of {5xx in last 30s → degraded,
   else ready}.
4. Subsystem precedence: **declared wins**; observed can only *downgrade* `llm`
   to `degraded` transiently and self-heals; observed never *upgrades*.

---

## (c) Poll loop — `web/src/health.ts` poller (`useHealthPoll`, started once in authed App)

Single shared poller (mirrors `ReviewBell` visibility handling, `Shell.tsx`):

- **Cadence:** 5s while any subsystem is `warming` (real-time feel as models come
  up — catches the only live transition fast); 20s steady; exponential backoff
  on unreachable (5→10→20→40→cap 60s, reset on success). Pause when hidden
  (`visibilitychange`); immediate poll on `focus`/`pageshow`/`online`.
- **AbortController 8s timeout** → treat as `server-unreachable`. Single-flight.
- **Auth without breaking offline-tolerance:** `authHeaders` is module-private
  (verified), and `api()` *throws* on 401 — which we must NOT let clear the key.
  Use a small raw fetch through the store that swallows 401 into
  `link` state and **never calls `clearAccessKey`** (the only logout remains
  `App.tsx:106` fed by `/api/auth/verify`). Concretely: export `authHeaders`
  (one line) and `fetch(u("/api/system/status"), {headers: authHeaders()})`,
  feeding outcomes through the same `report()` path. Unauthed/KeyEntry hits the
  public skeleton (200, no logout).
- Feeds `applySnapshot()` → declared subsystem states + `server.lastSeen`.

---

## (d) Real-time indicator UX — `Shell.tsx:243` (beside `ReviewBell`)

Status dot driven by `useHealth()`. The three-state **link** distinction is the
headline (today the app only knows `navigator.onLine`):

- **browser-offline** (gray): reuses existing offline banner (`Shell.tsx:261`).
- **server-unreachable** (red): online browser, server not answering → NEW banner
  "Can't reach {brain} — your connection is fine but the server isn't
  responding." (genuinely new; today: nothing).
- **subsystem-degraded** (amber): server reachable, a subsystem
  warming/degraded/unavailable; tap opens a panel (embeddings
  warming/ready/unavailable; audio ready/"not installed"; llm
  ready/"no key"/"errored — retrying").
- **all-green**: solid/hidden. Version banner stays (`Shell.tsx:258-261`).

Transitions still animate amber→green; with poll they arrive within one 5s
warming tick rather than instantly — which, for the one boot transition, is
indistinguishable to a human.

---

## (e) Pre-flight gating driven by the live model

`useCapability(name)` reads the store → `{enabled, reason, severity}`. Buttons
get `disabled` + tooltip (no blocking `alert`), flipping live as `warming→ready`
arrives on the next poll. Additive — never hides local-only capabilities.

| Feature | Gate | Disabled copy |
|---|---|---|
| Semantic search (`SearchPage`) | embeddings ready | "Search warming up — keyword still works." / "unavailable on this server." (force keyword; the semantic note/attachment path in `search.py:80-92` is **not** try/wrapped, so don't let it through while warming — it would block on `_get_model`) |
| Chat AI, note analysis, rebuild, research (`Chat`, `RebuildPanel`, …) | llm ready AND not server-unreachable | "AI needs an API key (none configured)." / "AI temporarily unavailable." |
| Transcribe (`Attachments.tsx:285`) | audio ready | "Transcription isn't installed on this server." (expected-off) |
| Image "Analyze with AI" (`Attachments.tsx:290`) | llm ready | existing gate + observed-llm degrade |
| Any write (capture/edits) | not offline/unreachable | "You're offline — changes can't be saved now." |

(Note the search-fallback correction adopted from red-team C-1a: semantic/hybrid
must be gated, not "let through assuming server degrades.")

---

## (f) Error surfacing tied to the bus

Lightweight toast in `Shell`, fed by `health.report({kind:"error",message})`.
Replace blocking `alert()` (`Chat.tsx:354,363,480,514-515,934`) with non-blocking
dismissible toasts (composer rollback stays). Silent `.catch(()=>{})` loads →
`report({kind:"silent-load-failed"})` → quiet "Couldn't load X" when server
healthy. A 5xx burst shows one coherent story (red dot + one de-duped toast).

---

## (g) Section 8 compliance

Offline-tolerant (poller never clears the key; 401 only flips `link`; logout
stays at `App.tsx:106`) · Cross-origin (bearer fetch via `u()`/exported
`authHeaders`; `allow_credentials` off) · No token burn (llm config-derived +
client-observed; never a model call per poll) · Cheap (in-memory snapshot, 20s
visible-only poll + backoff, paused when hidden — no long-lived socket) · Graceful
degradation (observed not forced; FTS works while embeddings warm; `unavailable`
= "feature off") · No heavy deps (native fetch + `useSyncExternalStore` + ~80-line
toast) · Security (status detail soft-auth-gated; public skeleton leaks only
`{ok,brain}`; nothing on the write-capable chat router).

---

## Ordered phases

1. Backend registry `health.py` (lock+dict, no event-loop machinery) + warmer
   wiring + audio `readiness()` keyed on `_model_key`.
2. Soft-auth `routers/system_status.py` (`GET /api/system/status`); register in
   `main.py:244`. curl-verifiable alone.
3. Client store + bus `health.ts`; reconciliation; `useHealth`. Export
   `authHeaders`.
4. **Observed feed** (the heart): add try/catch in `api()`, `streamChat`,
   `streamSSE`; wire `report()`; stall→re-poll. Client-only observed-llm downgrade.
5. Poll loop `useHealthPoll` (adaptive cadence, backoff, visibility, 8s timeout).
6. Indicator + banners + panel (`Shell.tsx`).
7. Gating (`useCapability`; SearchPage force-keyword fix, Chat, Attachments, composer).
8. Toasts replacing alerts/silent catches.
9. *(Deferred / only if multi-user)* SSE layer: `@sse2` Caddy block +
   `/api/system/status/stream` on the same router + one store input. **Not built now.**

## Testing strategy

**Backend:** `set_state` idempotency / transition stamping / lock-safety;
audio readiness flips `warming` after a `_model_key` change (simulate Settings
edit); snapshot shape; soft-auth route returns skeleton unauthed (no 401) and
full snapshot authed; assert no model load / no DB write per call.
**Client:** observed-feed unit tests (5xx→degraded, neterr→suspect, stall→re-poll,
401 still throws & never clears key); reconciliation truth table (offline /
neterr+no-byte→unreachable / 5xx→degraded / declared-ready beats observed-degraded
/ self-heal); adaptive cadence via fake timers; poll 401 doesn't logout; gating
warming→ready flips a button without reload; search forced to keyword while warming.
**Manual:** kill server mid-session → red dot, stays authed, recovers; cold boot
warming→ready within one 5s tick; cross-origin poll with bearer; no faster-whisper
→ audio "not installed," capture/search still work.

## Risks & tradeoffs (honest)

- **Poll is near-real-time (5–20s).** True push latency is gone — but for one
  user the only live transition is a boot event no human watches, so the cost is
  ~nil (this is the whole point of the SSE drop).
- **Steady tiny request volume** (20s visible-only) vs v1's single socket.
  Negligible, and it removes the socket/buffering/fallback complexity entirely.
- **Observed false positives:** a single transient 5xx flashes amber. Mitigated
  by short decay (30s) + declared-state precedence — trades stability for
  immediacy by design.
- **LLM "ready" = key present, not valid.** A revoked/over-quota key shows green
  until a real call fails — at which point the **client observed downgrade**
  catches it (the very gap v1's best idea closes). No token burn to pre-verify.
- **Audio reload edge:** keying on `_model_key` is correct but means a Settings
  model change briefly shows `warming` (accurate — it's re-downloading).
- **Soft-auth dual-depth route** is a small leak surface; covered by a dedicated
  "unauthed shape returns only {ok,brain}" test.

### Critical files

- `server/app/services/health.py` (new) · `server/app/services/audio_transcription.py` (`readiness()` keyed on `_model_key`) · `server/app/main.py` (warmers `:177`/`:208`, seed states, register router `:244`) · `server/app/routers/system_status.py` (new, soft-auth) · `web/src/health.ts` (new) · `web/src/api.ts` (add try/catch at `:40`, `:735`, `:806`; export `authHeaders`) · `web/src/components/Shell.tsx` (`:243` dot + banners) · `web/src/App.tsx` (start poll in authed subtree).

---

## ~150-word summary

v2 honestly takes the off-ramp the red team identified. The SSE stream is
**dropped**: under Caddy HTTP/2 the connection-cap justification is obsolete
(§1f), and for one user the only pushed transition is embeddings/audio flipping
ready ~5s after boot — caught by a 5s-while-warming poll, watched by nobody. The
plan now **leads with observed-health from real traffic** (the exercise's best
idea): try/catch added to `api()`, `streamChat`, and `streamSSE` (correcting v1 —
there was no existing catch around `streamChat`'s fetch at 735-740) feeds a
client store that reflects actual 5xx/network/stall outcomes between polls at zero
token cost. Health moves OFF the write-capable chat router onto a dedicated
soft-auth `/api/system/status`. The event-loop/`call_soon_threadsafe` registry
machinery is deleted (warmer state lands on the loop). The observed-LLM downgrade
is client-only (no cross-tab flapping). SSE is shelved as a clean, additive,
multi-user-only future layer.
