# Plan B v2 — Dedicated Status Endpoint + Heartbeat (red-team-hardened)

**Author:** Architect B (v2) · **Repo:** /home/user/JBrain · **Date:** 2026-06-07
**Supersedes:** `11-plan-B-status-endpoint.md` (v1)
**Critique addressed:** `20-redteam1-B.md`

This keeps the v1 philosophy the red team praised — a single typed, cached
**`GET /api/system/status`** endpoint with a five-state vocabulary, one ambient
**heartbeat**, the three-axis reachability model, one doc for *status* and
*gating*, and `StatusCtx` decoupled from auth — and surgically fixes the
load-bearing technical errors. It also steals Plan C's exhaustive gating
inventory + copy-exhaustiveness test and Plan D's client-side observed-outcome
signal (which replaces v1's invasive `llm.py` surgery).

---

## Changes from v1 / red-team responses

Every must-fix from `20-redteam1-B.md`, with the concrete resolution and the code
verification behind it.

| # | Red-team finding | v1 (wrong/under-specified) | v2 fix | Verified against |
|---|---|---|---|---|
| 1 | **HIGH — `authHeaders()` not exported; heartbeat won't compile; `api()` throws on 401** | `fetch(u(...),{headers:authHeaders()})` referenced a module-private symbol; `api()` throws `ApiError(401)`. | Add **one new exported, non-throwing helper `fetchStatus()`** in `api.ts` that does a raw `fetch` and returns `{status, body}` without ever throwing or touching `clearAccessKey`. Do **not** export `authHeaders` (keep it private); `fetchStatus` reads `getAccessKey()` itself. See §C0. | `api.ts:34` (`authHeaders` private), `api.ts:45` (`api()` throws 401), `api.ts:14` (`getAccessKey` exported). |
| 2 | **HIGH — `_last_auth_error` LLM hook grossly under-scoped** (no chokepoint; two provider classes; six entry points; sync+async; provider-specific exceptions) | A "module-level `_last_auth_error` set when a completion errors" — framed as a one-liner. | **DROPPED entirely.** Replace with Plan D's **client-side observed-outcome** signal: the existing central `api()`/`streamChat`/`streamSSE` already witness every 401/5xx/stall on real LLM traffic. A tiny client health bus records `llm` as `degraded` on an observed provider failure, self-healing on the next success. Zero `llm.py` changes. See §C4. | `llm.py` has `complete`/`complete_with_meta`/`complete_with_tools`/`stream_turn` across two classes (`:115,142,277,506`), none catching provider errors — confirmed; so the server hook is rejected. `api.ts:40-57`, `streamChat:712-777`, `streamSSE:803-838` are the real observation points. |
| 3 | **MEDIUM — "polled pre-auth on KeyEntry" contradicts the Shell-only mount** | Claimed the public skeleton is polled on KeyEntry, but the indicator lives in Shell, which only renders when authed. | **Reconciled by MOUNTING THE POLLER ABOVE THE AUTH GATE.** `StatusProvider` wraps the whole `<Routes>` tree in `App.tsx` (above the `!authed ? <KeyEntry/>` branch), so the heartbeat runs on KeyEntry too and the public skeleton has a real caller. The *indicator* still lives in Shell (authed); a minimal reachability line also renders on KeyEntry. The race with `loadInfo`/`connect` is specified in §C3. | `App.tsx:120-163` — `<AuthCtx.Provider>` wraps `<Routes>`; `!authed ? <KeyEntry/>` at `:127`; Shell at `:129`. |
| 4 | **MEDIUM — scheduler heartbeat blocks the event loop (thread-local SQLite on the loop thread)** | `set_meta(conn, "scheduler:last_beat", ...)` inline in the async loop. | **Kept, but corrected:** the `set_meta` runs **inside an `asyncio.to_thread`** like every other call in `_scheduler_loop`, using a `get_conn()` obtained *inside* that thread (thread-local correctness). Placed at the **top of each iteration body** (after the leading 60s sleep). Honest scope: it detects a *dead loop*, not a *wedged action* (items already run in threads). Marked **optional / lowest-value subsystem** — ship-or-cut flag. See §A4. | `main.py:63-103` (`_scheduler_loop`: leading `await asyncio.sleep(60)`, then each item via `asyncio.to_thread`), `db.py:82-88` (`get_conn` thread-local), `db.py:1242` (`set_meta(conn,key,value)`). |
| 5 | **MEDIUM — audio readiness must handle the model-reload branch (`_model_key != want`)** | "set `ready` after `_model` assigned" — ignored the Settings-driven reload. | Audio readiness is keyed off **`_model_key == want`**, not a one-shot flag. `readiness()` recomputes `want = (audio_model(), audio_compute_type())` and reports `warming` while `_model_key != want` (a Settings model-swap re-downloading), `ready` only when the cached key matches. See §A2. | `audio_transcription.py:97-110` (`want`; `_model_key != want` reload), `:46-53` (`audio_model`/`audio_compute_type` DB-meta overridable). |
| 6 | **HIGH — soft-auth dual-depth route is a leak surface; public `overall` leaks more than `/auth/info`** | One route, two depths, "one test"; public body included `overall` rollup. | **Decision: the status detail is AUTHED-ONLY (simpler, safer).** `GET /api/system/status` returns the **full document only with a valid key**. Pre-auth it returns an **exact, liveness-only skeleton `{ok, brain, ts}`** — *no* `overall`, *no* subsystem names — i.e. **no more than `/api/health` + `/auth/info` already expose** (`{ok,brain}` and `{brain_name}`). Built by **two separate builders** (`_public_skeleton()` vs `_full_snapshot()`), never strip-from-full. An exact-key-allowlist test locks the public body. See §B1. | `auth.py:58` (`verify_key(None)`→False, no throw), `auth.py:67` (`_extract_key`), `auth_router.py:15-18` (`/info`→`{brain_name}`), `main.py:254-256` (`/health`→`{ok,brain}`). |

**Additional steals (from the cross-plan critiques):**

- **From Plan C:** the exhaustive feature→capability inventory with real anchors
  (§E), and the **`CAP_COPY` exhaustiveness test** (§Testing). Also C's
  `warming`-vs-`unavailable` copy discipline and `disable+explain` over hide.
- **From Plan D:** the **observed-outcome health feed** (§C4) replacing the
  rejected server hook; client-side only, so one tab's blip never mutates global
  server state (the R2 hazard the D red team flagged).
- **From Plan A:** the explicit **multi-worker / per-process flag caveat** (§Risks)
  and the `unavailable`(package missing) vs `failed`(load error) **distinction**
  for embeddings/audio (different copy: "not installed" vs "failed to load").
- **From the C critique (1a):** the note/attachment **semantic search has no
  server-side FTS fallback** (`search.py:80-92` is un-wrapped) — so gating must
  **force keyword** while embeddings aren't `ready` (not trust a non-existent
  fallback). Wiring a `try/except` there is an optional backend hardening (§A1n).
- **From the D critique (1f):** SSE is **not** adopted (HTTP/2 makes the
  6-connection argument moot, but the value for a single user is still
  near-zero); polling + observed-outcome is the right cost/latency point. Named as
  a documented future option only.

**What is explicitly NOT real-time:** polling lags a transition by up to one
interval. The observed-outcome feed (§C4) closes the *server-reachability* and
*LLM-degraded* gaps between polls in near-real-time for free. The only remaining
poll-latency case is `warming→ready` on a fresh boot (seconds, observed by
nobody). We adopt **adaptive cadence** (5s while warming, 20s steady) to shrink
even that. We do not claim "real-time"; we claim "near-real-time, observed."

---

## Verification of research (confirmations against the actual code)

- `/api/health` liveness-only `{ok,brain}`: `main.py:254-256`. ✔
- `/api/auth/info` public `{brain_name}`; `/api/auth/verify` authed manifest:
  `auth_router.py:15-18, 22-38`. ✔
- Embeddings: `_model`/`_model_lock` `embeddings.py:16-17`; lazy `_get_model`
  `:20-30` (import of `fastembed` is *inside* `_get_model:25`); **never reloads**.
  Warmed via `asyncio.to_thread(embeddings._get_model)` `main.py:180,202`. ✔
- Audio: `_model`/`_model_key`/`_model_lock` `audio_transcription.py:38-40`;
  `_get_model` `:93-110` **reloads when `_model_key != want`** `:98`;
  `ImportError`→`TranscriptionUnavailable` `:103-107`; warmed `main.py:211,215`. ✔
- `_scheduler_loop` `main.py:63-103`: **leading** `await asyncio.sleep(60)` then
  each item via `asyncio.to_thread(lambda: ...(get_conn()))`; errors swallowed. ✔
- `get_conn()` is **thread-local** `db.py:82-88`; `set_meta(conn,key,value)`
  `db.py:1242`; `get_meta(key,default,conn)` `db.py:1250`. ✔
- Auth helpers: `verify_key(key|None)->bool` (no throw, `None`/empty→False)
  `auth.py:58-64`; `_extract_key(request)` `auth.py:67-71`. ✔
- Config: `has_anthropic`/`has_xai`/`has_llm` `config.py:71,75,80`. ✔
- Services: `geocode.enabled()` `geocode.py:38`; `push.public_key()` `push.py:67`;
  `llm.has_credentials()` `llm.py:506`; `entity_rebuild.status(conn)`
  `entity_rebuild.py:56` (**requires a `conn` arg**). ✔
- `COMPOSE_PROFILES` is **not** a settings prop — only read as
  `os.environ.get("COMPOSE_PROFILES","")` in `routers/system.py:253`; the
  aggregator must read `os.environ` directly. ✔ (fixes v1 LOW)
- Frontend: `api.ts:34` `authHeaders` **private**; `api.ts:14` `getAccessKey`
  exported; `api()` throws 401 `:45`; `streamChat` `:712-777` (no outer try/catch
  around the initial fetch `:735`); `streamSSE` `:803-838`; STALL_MS 90s
  `:752,815`. ✔
- App: `AuthCtx.Provider` wraps `<Routes>` `App.tsx:121`; `!authed?<KeyEntry/>`
  `:127`; Shell (authed) `:129`; sole 401-logout `App.tsx:106`. ✔
- Shell brand/top-bar + `ReviewBell` resume pattern (`visibilitychange`/`focus`/
  `pageshow`) `Shell.tsx:240-243, 38-57`. ✔
- CORS `allow_credentials` OFF, origins from `jbrain_cors_origins` default `*`,
  `expose_headers` `main.py:232-242`. ✔
- Single uvicorn worker (no `--workers`) → per-process flags fine today
  (documented caveat). ✔

---

## Core thesis (unchanged from v1)

Build ONE purpose-built, well-typed, cached status document at
**`GET /api/system/status`** aggregating every subsystem's readiness as an
explicit state machine, and drive the PWA from a single dedicated **heartbeat**.
The heartbeat distinguishes the three axes today's UI conflates:

- **browser-offline** (`navigator.onLine` / `offline` event — don't even fetch),
- **server-unreachable** (fetch rejects/times out/non-2xx, or an observed
  neterr/stall with no recent server byte),
- **server up but degraded** (200 with one or more non-`ready` subsystems).

Pre-flight gating reads the *same* document, so "what's broken" and "what's
disabled" share one source of truth.

Five-state vocabulary (kept — the critique praised it):

```
ready / warming / degraded / unavailable / unknown
```

`unavailable` carries a **kind** so copy can distinguish "feature not installed"
(`missing`) from "model failed to load" (`failed`) — see §A2 (stolen from A).

---

## (a) Backend readiness primitives

One `Capability` per subsystem: `{ state, detail?, kind?, last_checked }`.
All readiness is **observed, never forced** — `readiness()` reads cached
process state and never triggers a model load.

### A1. Embeddings readiness (`services/embeddings.py`)

Add module-level state beside `_model`/`_model_lock` (`:16-17`):

```python
import threading
_state: str = "unknown"          # unknown|warming|ready|unavailable
_state_detail: str | None = None
_state_kind: str | None = None   # "missing" | "failed" when unavailable
_state_lock = threading.Lock()

def _set_state(state, detail=None, kind=None):
    global _state, _state_detail, _state_kind
    with _state_lock:
        _state, _state_detail, _state_kind = state, (detail and str(detail)[:200]), kind

def readiness() -> dict:
    with _state_lock:
        return {"state": _state, "detail": _state_detail, "kind": _state_kind}
```

Wire inside `_get_model()` (`:20-30`): set `warming` on entry to the load block;
`ready` immediately after `_model` is assigned (`:29`); on `ImportError`
(fastembed missing) `_set_state("unavailable", str(exc), kind="missing")` and
re-raise; on any other exception `kind="failed"`. The warm task (`main.py:177`)
already calls `_get_model`, so a healthy box flips `warming→ready` within seconds
of boot with **no extra work**; the read path is a cheap locked dict read.
Embeddings **never reload**, so a one-shot flag is correct here.

**A1n (optional backend hardening — from C critique 1a):** wrap the two
note/attachment semantic calls in `search.py:80-92` in `try/except` so a
`warming`/`unavailable` embeddings state degrades to FTS server-side instead of
blocking/500-ing. Independent of this plan; the UI gate (§E) forces keyword
regardless, so this is belt-and-suspenders.

### A2. Transcription readiness (`services/audio_transcription.py`) — reload-aware

Same shape, but keyed off the **reload condition**, not a one-shot flag:

```python
_state: str = "unknown"
_state_detail: str | None = None
_state_kind: str | None = None
_state_lock = threading.Lock()

def readiness() -> dict:
    # Cheap, non-blocking. Reports the model the SETTINGS currently want, so a
    # Settings-driven model swap (which forces a re-download) reads as 'warming'
    # until the cached _model_key matches that want — never a stale 'ready'.
    want = (audio_model(), audio_compute_type())
    with _state_lock:
        st, detail, kind = _state, _state_detail, _state_kind
    if st == "unavailable":                       # package missing — sticky
        return {"state": st, "detail": detail, "kind": kind,
                "model": want[0], "compute_type": want[1]}
    if _model is not None and _model_key == want:
        return {"state": "ready", "detail": None, "kind": None,
                "model": want[0], "compute_type": want[1]}
    # _model is None (cold) OR _model_key != want (Settings changed → reloading)
    return {"state": st if st in ("warming", "unknown") else "warming",
            "detail": detail, "kind": kind,
            "model": want[0], "compute_type": want[1]}
```

Wire inside `_get_model()` (`:93-110`): set `warming` on entry to the load block;
on the `ImportError`→`TranscriptionUnavailable` branch (`:103-107`)
`_set_state("unavailable", "faster-whisper not installed", kind="missing")`;
on any other `WhisperModel(...)` failure `kind="failed"`. After `_model_key`
is set (`:109`), `readiness()` derives `ready` purely from `_model_key == want`
— so the **reload branch is handled** (this was the v1 bug). `_model_key != want`
during a Settings swap correctly reads `warming`. Use
`importlib.util.find_spec("faster_whisper")` (cheap, no load) only when state is
still `unknown`, so the PWA can gate audio **before** the first attachment.

### A3. Other subsystems (config-time, cheap; read per poll)

| Subsystem | Source (verified) | `ready` when |
|---|---|---|
| `db` | `get_conn().execute("SELECT 1")` | succeeds (proves WAL writable-ish/reachable) |
| `llm.anthropic` | `settings.has_anthropic` (`config.py:71`) | key present |
| `llm.xai` | `settings.has_xai` (`config.py:75`) | key present / provider xai\|grok |
| `push` | `push.public_key()` (`push.py:67`) | non-empty VAPID public key |
| `geocoder` | `geocode.enabled()` (`geocode.py:38`) | URL configured |
| `embeddings` | `embeddings.readiness()` (§A1) | state==ready |
| `transcription` | `audio_transcription.readiness()` (§A2) | state==ready |
| `entity_rebuild` | `entity_rebuild.status(conn)` (`:56`, **needs conn**) | idle/rebuilding (degraded on `last_error`) |
| `image_analysis` | `llm.has_credentials()` (`llm.py:506`) | LLM key present |
| `scheduler` | `get_meta("scheduler:last_beat")` (§A4) | last beat < 180s ago |
| `update_sidecar` | `os.environ.get("COMPOSE_PROFILES","")` (**not** settings) | `"autoupdate"` present |

**LLM cost discipline (kept):** **never** a model call per poll. Key-present →
`ready`. The "present-but-revoked key" gap is closed *after first use* by the
client observed-outcome feed (§C4), which marks `llm` `degraded` on an observed
401/billing/5xx from real chat traffic and self-heals on the next success — at
zero token cost, and **without** the rejected `llm.py` surgery.

### A4. Scheduler heartbeat (corrected, optional)

In `_scheduler_loop` (`main.py:63-103`), add a heartbeat write **inside a
`to_thread`** at the top of each iteration body (after the leading 60s sleep):

```python
while True:
    await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
    try:
        # Thread-local SQLite: get_conn() + set_meta MUST run on the worker
        # thread, never inline on the event loop (db.py:82-88, main.py to_thread pattern).
        await asyncio.to_thread(
            lambda: set_meta(get_conn(), "scheduler:last_beat", _iso_now()))
    except Exception:  # noqa: BLE001
        pass
    # ... existing run_due_scheduled / location triggers / trips / rebuild / image cleanup ...
```

**Honest scope:** this detects a **dead loop** (the asyncio task died — rare),
not a **wedged action** (an item hangs without raising — the realistic case, but
items already run in their own threads so the loop keeps beating). Marginal value
for the wiring cost. **Ship-or-cut flag: this subsystem is the lowest value of
the eleven; cut it if review prefers minimalism.** If cut, drop the `scheduler`
row from §A3 and the aggregator.

### A5. Aggregator (`server/app/services/system_status.py`, new)

`snapshot(conn) -> dict` assembles `{state, detail, kind?, last_checked}` per
subsystem plus a top-level `overall` rollup (`ready` / `degraded` / `down`).
**Core = `db` only** — every other subsystem degrades gracefully, so a missing
LLM key is `degraded`, not `down`. `entity_rebuild.status(conn)` is passed the
`conn` it requires (`:56`). Cheap: in-memory reads + one trivial `SELECT 1` +
config-prop reads.

---

## (b) The aggregated status endpoint

### B1. Authed-only detail; liveness-only public skeleton (TWO builders)

**Security decision (resolves red-team HIGH #6):** the detailed document is
**authed-only**. Pre-auth, the route returns an exact, liveness-only skeleton —
**no `overall`, no subsystem names** — leaking no more than `/api/health`
(`{ok,brain}`) and `/api/auth/info` (`{brain_name}`) already do.

New lightweight router `routers/system_status.py` (NOT on the owner-gated
`system` router, which hard-401s — that would break the offline-tolerant
heartbeat). A **soft-auth** dependency that never raises:

```python
# routers/system_status.py
from fastapi import APIRouter, Request
from ..auth import verify_key, _extract_key      # auth.py:58, :67 — both importable, no throw
from ..config import get_settings
from ..db import get_conn
from ..services import system_status
from ..clock import iso_now                       # or datetime.now(UTC).isoformat()

router = APIRouter(prefix="/api/system", tags=["system-status"])

def _public_skeleton() -> dict:                   # builder #1 — exact allowlist
    return {"ok": True, "brain": get_settings().brain_name, "ts": iso_now()}

@router.get("/status")
def status(request: Request):
    if not verify_key(_extract_key(request)):     # None/empty/bad → False, never throws
        return _public_skeleton()                 # liveness only, no rollup, no names
    return system_status.snapshot(get_conn())     # builder #2 — full doc (authed)
```

Register in the `main.py:244` router loop (add `system_status`).

**Hardening (resolves the "strip-from-full" leak vector):** `_public_skeleton()`
and `system_status.snapshot()` are **two independent builders**. The public path
**never** builds the full dict and trims it. A test asserts
`set(public_body.keys()) == {"ok","brain","ts"}` **exactly** (§Testing), so a
future refactor that leaks a key fails CI.

### B2. Full document (authed)

```json
{
  "ok": true, "brain": "My Brain", "version": "1.42.0", "ts": "...Z",
  "overall": "degraded",
  "capabilities": {
    "db":            { "state": "ready" },
    "llm": {
      "anthropic":   { "state": "ready", "detail": "key present" },
      "xai":         { "state": "unavailable", "detail": "no XAI_API_KEY", "kind": "missing" }
    },
    "embeddings":    { "state": "warming", "detail": "loading bge-small-en-v1.5" },
    "transcription": { "state": "unavailable", "detail": "faster-whisper not installed",
                       "kind": "missing", "model": "base", "compute_type": "int8" },
    "push":          { "state": "ready" },
    "geocoder":      { "state": "ready", "detail": "nominatim..." },
    "scheduler":     { "state": "ready", "detail": "last beat 12s ago" },
    "entity_rebuild":{ "state": "ready" },
    "image_analysis":{ "state": "ready" },
    "update_sidecar":{ "state": "unavailable", "detail": "autoupdate profile off", "kind": "missing" }
  }
}
```

Note: the `overall` rollup lives **only in the authed body** now (v1 leaked it
publicly). `llm` has **no** `last_error` sub-object — that signal is now
client-side observed (§C4), not a server field.

### B3. Types

Backend Pydantic `CapState` enum + `Capability` model.
Frontend `web/src/status.ts`:

```ts
export type CapState = "ready"|"warming"|"degraded"|"unavailable"|"unknown";
export type CapKind = "missing"|"failed";
export interface Capability {
  state: CapState; detail?: string|null; kind?: CapKind|null;
  last_checked?: string; model?: string; compute_type?: string;
}
export interface SystemStatus {
  ok: boolean; brain: string; version?: string; ts: string;
  overall?: "ready"|"degraded"|"down";              // authed only; absent on skeleton
  capabilities?: Record<string, Capability | Record<string, Capability>>;
}
```

### B4. Caching

(1) In-process micro-cache in `snapshot()`, TTL `STATUS_TTL=3s` — a **dedicated
3s polling cache** (NOT the GitHub-release 1h cache at `system.py:32`; v1's
citation was wrong, the *shape* is borrowed, the TTL is ours).
(2) `Cache-Control: no-store` + optional `ETag`→`304`. Cost per call: one
`SELECT 1` + in-memory reads; **no model load, no network, no tokens.**

---

## (c) Heartbeat hook (`web/src/heartbeat.ts`, new) + status store

### C0. The new non-throwing fetch helper (resolves red-team HIGH #1)

`authHeaders` stays **private**. Add one exported helper to `api.ts` that the
heartbeat uses — it never throws and never calls `clearAccessKey`:

```ts
// api.ts — NEW export. Raw, non-throwing; for the heartbeat's offline-tolerant poll.
export async function fetchStatus(path: string, signal?: AbortSignal):
  Promise<{ status: number; body: any | null }> {
  const headers: Record<string,string> = { "Content-Type": "application/json" };
  const k = getAccessKey();                      // api.ts:14, already exported
  if (k) headers["Authorization"] = `Bearer ${k}`;
  const res = await fetch(u(path), { headers, signal });   // may reject → caller catches
  let body: any = null;
  try { body = await res.json(); } catch { /* skeleton/no-body */ }
  return { status: res.status, body };
}
```

This is the **only** `api.ts` change required for the heartbeat. It does not
disturb `api()`'s 401-throw contract, so the sole 401-logout site
(`App.tsx:106`, fed by `/api/auth/verify`) is untouched — the offline-tolerant
invariant is preserved by construction.

### C1. Hook + shared store

`useHeartbeat()` over a module singleton store so multiple components share ONE
poller (mirrors the `ReviewBell` resume pattern, `Shell.tsx:38-57`).

```ts
type Reachability = "online"|"server-unreachable"|"browser-offline";
interface Heartbeat {
  reachability: Reachability;
  status: SystemStatus | null;
  overall: "ready"|"degraded"|"down"|"unknown";
  observed: { llmDegradedAt: number | null; last5xxAt: number | null;
              lastNetErrAt: number | null };       // from §C4
  lastOkAt: number | null; stale: boolean; refreshNow: () => void;
}
```

### C2. Reachability + cadence

- `!navigator.onLine` → `browser-offline` (don't fetch).
- `fetchStatus` rejects / times out / `status>=500` / `status===0` →
  `server-unreachable`. **A `401` from the status route is impossible** (the route
  is soft-auth and returns the public skeleton instead), so there is no
  401-as-unreachable confusion (the bug Plan A had).
- `2xx` → `online`; then `overall` (authed) drives `degraded`.
- **Observed-outcome override (§C4):** a recent neterr/stall from real traffic
  with no server byte within ~8s flips to `server-unreachable` between polls
  (near-real-time server-down detection).

**Adaptive cadence (stolen from A):** 5s when any subsystem is `warming`; 20s
when ready+visible; exponential backoff on unreachable (5→10→20→40→cap 60s,
reset on success). Pause when hidden (`visibilitychange`); immediate poll on
visible/focus/`online`/`pageshow`; `offline` event → `browser-offline` without
fetch. **AbortController 8s timeout** → `server-unreachable`. Single-flight.
Cadence state lives in a **ref** so a `warming→ready` transition doesn't tear
down/rebuild the interval (avoids the request-churn the A critique flagged).

### C3. Mount point + KeyEntry reconciliation (resolves red-team MEDIUM #3)

`StatusProvider` wraps the **entire `<Routes>` tree** in `App.tsx` (just inside
`<AuthCtx.Provider>` at `:121`, **above** the `!authed ? <KeyEntry/>` branch at
`:127`). Consequences:

- The heartbeat **runs on KeyEntry**, so the public skeleton has a real caller —
  reconciling the v1 contradiction. On KeyEntry the device can now distinguish
  "server unreachable" from "browser offline" *before login* (a genuine win:
  today KeyEntry shows nothing).
- The **detail indicator still lives in Shell** (authed, `Shell.tsx:243` beside
  `ReviewBell`); a **minimal one-line reachability banner** also renders on
  KeyEntry ("Can't reach <brain>" vs "You're offline").
- **Race with `loadInfo`/`connect` (`App.tsx:97-111`):** the heartbeat is
  independent of the boot `/auth/verify`. Both may fire near-simultaneously on a
  fresh load; that's fine — they hit different routes, neither mutates the other's
  state, and `fetchStatus` never calls `clearAccessKey`. The heartbeat does **not**
  drive auth; auth still flips only on the `App.tsx:106` 401 path. On `connect()`
  (key entered), the store calls `refreshNow()` so the first authed poll fires
  immediately rather than waiting for the next tick.

### C4. Observed-outcome feed (steals Plan D; replaces the rejected `llm.py` hook)

The central wrappers already witness every real failure. Feed them into the
status store — **client-side only**, so no server state is mutated by one tab
(avoiding the D-critique R2 hazard) and **no `llm.py` change is needed**.

- In `api()` (`api.ts:40-57`): wrap the `fetch` in try/catch; on a thrown
  network error `store.report({kind:"neterr"})`; on `res.status>=500`
  `store.report({kind:"http5xx"})`. **401 still throws unchanged** → `App.tsx:106`
  untouched. (This is additive; `api()`'s return/throw behavior is identical.)
- In `streamChat` (`api.ts:712-777`): there is **no outer try/catch around the
  initial `fetch` at `:735`** (verified — the D critique's correction), so we
  **add** one to `report({kind:"neterr"})` on a failed POST; the stall watchdog
  abort (`STALL_MS`, `:752`) reports `{kind:"stall"}`; a `{type:"error"}` chat
  event reports `{kind:"llm-error"}`.
- In `streamSSE` (`api.ts:803-838`): same — add a try/catch around the initial
  `fetch` at `:806` and on the `{type:"error"}` rebuild event.

**Reconciliation rules:** server-declared state (the poll) is authoritative and
can only be *downgraded* transiently by observed signals; observed never
*upgrades*. `llm-error` → `llm` shown `degraded` for ~60s, self-healing on the
next successful turn or the next poll. `neterr`/`stall` with no server byte in
~8s → `server-unreachable`. A single `http5xx` → server `degraded` for ~30s.
This delivers the "present-but-revoked key" warning **after first use** at zero
token cost — exactly what v1's invasive server hook was trying to do, far more
cheaply.

---

## (d) Status indicator UX

`web/src/components/StatusIndicator.tsx`, mounted in `Shell.tsx:243` (top bar,
beside `ReviewBell`):

| Condition | Dot | Label |
|---|---|---|
| browser-offline | gray | "Offline" |
| server-unreachable | red | "Server unreachable" |
| online + ready | green/hidden | none |
| online + degraded | amber | "Some features limited" |
| any warming | blue pulse | "Starting up…" |

Keep the version-mismatch banner. **Replace** the `navigator.onLine`-only offline
banner (`Shell.tsx:258-261`) with one driven by `reachability`, so "server
unreachable while device online" is finally distinguishable. Tapping the dot
opens a detail panel (reuse `Modal.tsx`) listing each capability with dot +
`detail` + `kind`-aware copy + `last_checked`, remediation hints, and a Refresh
button (`refreshNow()`). Link from `SystemPage`. New CSS vars
`--status-ok/warn/down/off/warming`. On **KeyEntry**, only the minimal
reachability line renders (no capability detail pre-auth).

---

## (e) Systematic pre-flight gating (exhaustive — steals Plan C)

Sibling `StatusCtx` (keep auth/health decoupled — the critique praised this);
`useCapabilities()` returns typed booleans, falling back to `auth.hasLlm`
(`App.tsx:116`) before the doc loads. Single-sourced copy table `CAP_COPY`
(stolen from C), one entry per `(capId, state)`, with a CI exhaustiveness test
(§Testing). `warming` copy is "try again shortly"; `unavailable` copy branches on
`kind` ("not installed" vs "failed to load").

**Exhaustive inventory** (every route in `App.tsx:131-156` + the action controls
inside, with anchors; corrected for the errors the C critique found):

| Feature | Anchor | Gate | Degrade copy |
|---|---|---|---|
| Image "Analyze with AI" | `Attachments.tsx:290` | `llmReady` | "AI analysis needs an LLM key." |
| Transcribe (audio/video) | `Attachments.tsx:285` | `transcriptionReady` | `missing`→"Transcription isn't installed on this server." / `failed`→"Speech model failed to load." / `warming`→"Loading model — try again shortly." |
| Attachments help copy | `Attachments.tsx:197` | `llmReady` | drop "summarized by AI" clause when no key |
| Chat **Entry** mode (Generic/Medical/Financial) | `Chat.tsx:537-575` | online only | never LLM-gated (local capture) |
| Chat **Research** / **Full Brain** modes | `Chat.tsx:669` | `llmReady` + online | disable mode segment; composer note "Assistant needs an LLM key." |
| Chat **Send** | `Chat.tsx:946-947` | online + (`llmReady` if chat mode) | extend existing `!online` disable with `mode!=="entry" && !llmReady` |
| Research **Deep** toggle | `Chat.tsx:939-941` | inside gated Research mode | hidden transitively |
| Lab extraction on PDF upload | `Chat.tsx:558-562` | `llmReady` | note near Medical sub when no key |
| Research approve/skip proposal | `Chat.tsx:713-724` | inside gated Research mode | covered transitively |
| **Search** keyword | `SearchPage.tsx` (live-as-you-type) | none (FTS) | always available — the safe default |
| **Search** semantic/hybrid | `SearchPage.tsx` MODES | `embeddingsReady` | **force keyword** while not ready (the server has NO FTS fallback for the note/attachment semantic path — C critique 1a); `warming`→"semantic starting up — keyword only" |
| Note **AI Analysis** ↻ | `AiAnalysisPanel.tsx:27-40` | `llmReady` | "AI analysis off (no LLM key)." |
| Note **Rebuild/Draft/Regather/Guide/Redraft** | `RebuildPanel.tsx:5-9` | `llmReady` (+ embeddings for gather quality) | gate entry button; note "keyword-only sources" if embeddings down |
| Note **Talk** (KB pages) | `NotePage.tsx` (`TalkPanel`) | `llmReady` | gate send control |
| **Owner-assisted chat** route | `App.tsx:138` (`OwnerChatPage`) | `llmReady` | whole-route note "AI needs an LLM key." |
| Entity rebuild / research / KB synth | `EntitiesPage`, research | `llmReady` (+ embeddings) | "Needs an LLM key." (C critique 1e: rebuild IS llm+embeddings, not "nothing extra") |
| Push subscribe | Settings / `Shell.tsx:15-93` | `pushReady` | "Push unavailable: no VAPID key." |
| Map address labels | `MapPage.tsx` | `geocoderReady` | tiles/trail render regardless; labels "coordinates only" — small surface (C critique 1d) |
| **Public share** chat (`/share/:token`) | `SharePage.tsx` → `GuidedChat`/`ResearchChat` | **out of scope** (no auth context, outside `StatusProvider`'s authed data) | the *owner* is warned at share-creation time via the authed `llmReady`; the public page itself can't read the manifest. Explicitly scoped out (C critique 1b). |
| Any LLM/embeddings action while unreachable | global | `reachability` | "Server unreachable" tooltip + toast |

Mechanism: declarative `web/src/components/Gated.tsx` (renders enabled when
`when`; else a disabled control with `title`/`aria-disabled` + reason, plus a
spinner + "Starting…" when `warming`):

```tsx
<Gated when={transcriptionReady} reason={capReason("transcription")}>
  <button onClick={transcribe}>Transcribe</button>
</Gated>
```

A checklist comment at the top of `status.ts` and `AdvancedHome.tsx` points here
(drift mitigation).

---

## (f) Richer in-the-moment error surfacing

`web/src/components/Toast.tsx` + `useToast()` (~80 lines, no dep). Replace
blocking `alert()`s in Chat/NotePage/Attachments with toasts including server
`detail` (`api.ts:46-54`). Enrich `ApiError` with an optional
`category` ("auth"|"network"|"unavailable"|"validation"|"server") inferred from
status (stolen from C/D — cleaner than v1's ad-hoc `kind:"network"` tag), so the
toast layer and the heartbeat agree on classification. A degraded action that
slipped past gating reads the live status doc for a specific message. Convert key
silent `.catch(()=>{})` loaders into quiet toasts when the server is healthy.
Keep the SSE 90s stall watchdog; on stall, toast + mark `stale` + `refreshNow()`.
The §C4 observed feed and the toast layer share one classification.

---

## (g) Constraint compliance (research §8)

- **Offline-tolerant** ✅ heartbeat uses `fetchStatus` (never throws, never
  `clearAccessKey`); 401≠logout (sole site `App.tsx:106` untouched); observed
  feed leaves `api()`'s 401-throw intact.
- **Cross-origin** ✅ bearer via `getAccessKey()`/`u()`; CORS `*`/bearer,
  `allow_credentials` off (`main.py:232-242`); no cookies, no `credentials:include`.
- **No token burn** ✅ key-presence only; observed-outcome rides real traffic;
  zero synthetic model calls.
- **Cheap & frequent** ✅ in-memory reads + one `SELECT 1` + 3s cache; 20s/5s
  adaptive, paused-when-hidden, 8s abort, single-flight.
- **Graceful degradation** ✅ observed not forced; keyword search forced (not a
  phantom server fallback); `warming` vs `unavailable(kind)` honest copy.
- **No heavy deps** ✅ hand-rolled toast/store; native fetch.
- **Security** ✅ public skeleton is exactly `{ok,brain,ts}` (two-builder,
  allowlist-tested), no more than `/health`+`/auth/info`; detail authed-only.

---

## Ordered phases

1. **Backend primitives:** `embeddings.readiness()` (§A1), reload-aware
   `audio_transcription.readiness()` (§A2), `system_status.snapshot()` aggregator
   (§A5), scheduler heartbeat (§A4, or cut). Tests.
2. **Endpoint:** `routers/system_status.py` soft-auth, two-builder (§B1);
   register in `main.py:244`; ETag/304; tests incl. public-key-allowlist.
3. **Client core:** `fetchStatus()` in `api.ts` (§C0); `status.ts` types;
   `heartbeat.ts` store/hook with adaptive cadence + abort (§C1-C2);
   `StatusProvider` mounted above the auth gate (§C3); `StatusCtx`/`useCapabilities`.
4. **Observed feed (§C4):** instrument `api()`/`streamChat`/`streamSSE`
   (add the missing outer try/catch around the stream fetches).
5. **Indicator + UX:** `StatusIndicator` + Modal panel in Shell; KeyEntry
   reachability line; replace the offline banner; `Toast`/`useToast` +
   `ApiError.category`.
6. **Gating sweep (§E):** `Gated.tsx` + `CAP_COPY`; roll out transcription →
   search(force-keyword) → chat modes/send → note AI/rebuild → push → map →
   owner-chat. Drift checklist comments.

---

## Testing strategy

**Backend:**
- `snapshot()` returns all expected keys + valid states; **no model load when
  cold** (assert `embeddings._model is None` / `audio._model is None` after a
  call pre-warm).
- **Public skeleton EXACT allowlist:** unauthed `GET /api/system/status` body
  keys `== {"ok","brain","ts"}` exactly — and asserts `overall` and
  `capabilities` are ABSENT (locks the §B1 security boundary; this is the
  structural guarantee the v1 "one test" lacked).
- Authed body has `overall` + `capabilities`; `verify_key(None)`→skeleton path.
- Embeddings transitions: `unknown→warming→ready`; forced `ImportError`→
  `unavailable kind=missing`; other exception→`kind=failed`.
- Audio **reload branch**: set `_model`+`_model_key`; change `audio_model()`
  via `meta`; assert `readiness()` reports `warming` (not stale `ready`) until
  `_model_key==want` again. (Directly tests the v1 bug fix.)
- Scheduler heartbeat (if kept): `set_meta` runs and `readiness` flips
  `ready→degraded` past 180s; assert the write path is `to_thread`-wrapped (no
  event-loop SQLite).
- Micro-cache: two calls within 3s share one snapshot.

**Frontend:**
- `fetchStatus` never throws on 401/5xx/neterr; returns `{status,body}`.
- Reachability matrix (offline / unreachable / online+degraded / online+ready).
- Backoff sequence + reset; adaptive 5s-while-warming; pause-on-hidden,
  resume-on-focus/`pageshow`/`online`; 8s AbortController → unreachable;
  single-flight; cadence-in-ref (no interval thrash on `warming→ready`).
- **Heartbeat 401 cannot occur** (soft-auth) and the store **never** calls
  `clearAccessKey` (spy).
- Observed feed: `api()` 5xx → `degraded`; neterr+no-byte-8s → unreachable;
  `llm-error` → `llm degraded` self-healing; pushed-`ready` beats observed; 401
  still throws from `api()` unchanged.
- `Gated` disabled + reason + spinner + `aria-disabled`; search forces keyword
  while embeddings not ready.
- **`CAP_COPY` exhaustiveness test (stolen from C):** fails if any `(capId,
  reachable state)` lacks copy — guards drift.

**Manual:** cross-origin pre-login skeleton on KeyEntry (distinguish unreachable
vs offline before auth); kill server mid-session → red within ~8s via observed
feed, stays authed, recovers; cold boot `warming→ready` with keyword-only search;
revoke LLM key, send a chat → `llm` flips `degraded` after the failure (observed),
green again after a valid call.

---

## Risks & tradeoffs (honest)

- **Near-real-time, not real-time.** Poll lags `warming→ready` up to one interval
  (5s while warming). Mitigated by the observed feed for the cases that actually
  matter mid-session (server-down, LLM-degraded). SSE rejected: HTTP/2 neutralizes
  the connection-cap argument, but for a single self-hosted user the pushed
  transition (boot readiness) is observed by nobody. Documented future option.
- **Multi-worker / per-process flags (stolen caveat from A).** Readiness lives in
  process memory. JBrain runs a **single uvicorn worker today** (no `--workers`),
  so this is latent — but if anyone adds workers behind Caddy, embeddings/audio
  readiness would flicker per worker. Loudly documented; a shared store (meta
  table) would be the fix.
- **LLM "ready" is presence-only until first use.** A revoked/over-quota key
  shows green until a real call fails; the observed feed (§C4) then flips it
  `degraded`. Accepted (cost); strictly better than v1's "green forever" without
  the rejected surgery.
- **Observed false positives.** A single transient 5xx briefly shows `degraded`;
  short decay + server-declared precedence mitigate. Client-side only, so no
  global/multi-tab state corruption (the D-critique R2 hazard is avoided).
- **Scheduler heartbeat is low-value** — detects a dead loop, not a wedged action.
  Cut-flagged.
- **Gating drift** — exhaustive table needs maintenance; mitigated by single-
  sourced `CAP_COPY`, the exhaustiveness test, `Gated` making a gate ~1 line, and
  checklist comments. The toast layer (§f) is the backstop for any miss.
- **Toast is net-new surface** — kept minimal, confined to replacing `alert()`s.

### Critical files
- `server/app/services/embeddings.py`, `server/app/services/audio_transcription.py`
- `server/app/services/system_status.py` (new), `server/app/routers/system_status.py` (new)
- `server/app/main.py` (register router; scheduler heartbeat)
- `web/src/api.ts` (`fetchStatus` export; observed-feed instrumentation)
- `web/src/heartbeat.ts` (new), `web/src/status.ts` (new),
  `web/src/components/StatusIndicator.tsx`, `Gated.tsx`, `Toast.tsx` (new)
- `web/src/App.tsx` (mount `StatusProvider` above the auth gate)
