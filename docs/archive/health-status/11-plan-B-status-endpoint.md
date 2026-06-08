# Plan B — Dedicated Status Endpoint + Client Heartbeat

**Author:** Architect B · **Repo:** /home/user/JBrain

## Verification of research (corrections/confirmations)

- `/api/health` liveness-only: `main.py:254-256` returns `{"ok","brain"}`.
- `/api/auth/verify` de-facto manifest: `routers/auth_router.py:22-38`.
- Embeddings warm async, no readiness flag: `main.py:177-202`; lazy `_get_model()` `services/embeddings.py:20-30`.
- Transcription warm async/non-fatal: `main.py:208-215`; `_get_model()` raises `TranscriptionUnavailable` `services/audio_transcription.py:93-110`.
- Offline-tolerant auth: `App.tsx:97-111` — only `e?.status===401` clears the key.
- `useOnline` is `navigator.onLine`-only: `hooks.ts:264-277`.
- Banners at `Shell.tsx:258-261`; gating only in `Attachments.tsx:38`.
- No toast system. CORS bearer-only, `allow_credentials` off, origins from `JBRAIN_CORS_ORIGINS` (`main.py:226-242`).
- Precedent for typed status doc: `entity_rebuild.status()` (`services/entity_rebuild.py:56-65`).
- `AuthState` carries little: `App.tsx:33-45`.

## Core thesis

Build ONE purpose-built, well-typed, cached status document at `GET /api/system/status` aggregating every subsystem's readiness as an explicit state machine, and drive the PWA from a single dedicated **heartbeat hook**. The heartbeat distinguishes three axes the current UI conflates: **browser offline** (`navigator.onLine`) vs **server unreachable** (fetch fails/times out) vs **server up but degraded** (200 with non-ready states). Pre-flight gating reads the same document, so "what's broken" and "what's disabled" share one source of truth.

---

## (a) Backend readiness state machines

Shared vocabulary, one `CapState` per subsystem:
```
ready / warming / degraded / unavailable / unknown
```

### A1. Embeddings readiness (`services/embeddings.py`)

Add module-level state (mirrors `_model`/`_model_lock` at lines 16-17):
```python
_state = "unknown"
_state_detail: str | None = None
_state_lock = threading.Lock()
def readiness() -> dict: return {"state": _state, "detail": _state_detail}
def _set_state(state, detail=None):
    global _state, _state_detail
    with _state_lock: _state, _state_detail = state, detail
```
Wire: top of `_get_model()` set `warming`; after `_model` assigned set `ready`; on exception set `unavailable` with `str(exc)[:200]` and re-raise. In `_warm_embeddings` set `warming` before the thread call. Readiness is observed, never forces work.

### A2. Transcription readiness (`services/audio_transcription.py`)

Same pattern (lines 38-40). `readiness()` returns `{state, detail, model, compute_type}`. `ImportError` branch (line 103) → `unavailable`. Use `importlib.util.find_spec("faster_whisper")` (cheap, no load) when state still `unknown` so PWA can gate audio BEFORE first attachment.

### A3. Other subsystems (config-time, cheap; read per poll)

| Subsystem | Source | ready when |
|---|---|---|
| `llm.anthropic` | `settings.has_anthropic` | key present |
| `llm.xai` | `settings.has_xai` | key present / provider xai|grok |
| `push` | `push.public_key()` | non-empty VAPID public key |
| `geocoder` | `geocode.enabled()` | URL configured |
| `db` | `SELECT 1` | succeeds |
| `scheduler` | new heartbeat ts in `_scheduler_loop` | last beat < 180s |
| `update_sidecar` | `"autoupdate" in COMPOSE_PROFILES` | profile present |
| `entity_rebuild` | `entity_rebuild.status()` | idle/rebuilding |
| `image_analysis` | `llm.has_credentials()` | LLM key |

LLM nuance (cost): **never** a model call per poll. `key present → ready`; `key present + last real call 401/billing-failed → degraded` via a module-level `_last_auth_error: tuple[ts,msg]|None` in `services/llm.py`, set when a real completion errors. Read by status, never probed.

Scheduler heartbeat: in `_scheduler_loop` (`main.py:63`) `set_meta(conn,"scheduler:last_beat",iso_now)` each successful iteration. Detects a wedged loop.

### A4. Aggregator (`server/app/services/system_status.py`, new)

`snapshot(conn)` assembles `{state,detail,last_checked}` per subsystem + a top-level rollup `overall` (`ready`/`degraded`/`down`). Core = `db` only (everything else degrades gracefully). Cheap: in-memory reads + one trivial SELECT.

---

## (b) The aggregated status endpoint

### B1. Public skeleton + authed detail (one route, soft-auth)

Constraint: don't leak capability detail pre-auth. **One route, auth-aware depth.**

`GET /api/system/status` (public): `{ "ok":true, "brain":"...", "overall":"ready", "ts":"..." }` — liveness + coarse rollup, NO subsystem names. Polled by heartbeat even pre-auth (KeyEntry).

`GET /api/system/status?detail=1` with valid key → full doc. Implement via soft-auth dependency `optional_key(request) -> bool` wrapping `verify_key(_extract_key(request))` (`auth.py:58`), never raises. New lightweight router `routers/system_status.py` (no router-level 401, so the public heartbeat + offline-tolerant contract survive). Not on the owner-gated `system` router which hard-401s.

### B2. Full document (authed)

```json
{
  "ok": true, "brain": "My Brain", "version": "1.42.0", "ts": "...Z",
  "overall": "degraded",
  "capabilities": {
    "db":            { "state": "ready" },
    "llm": {
      "anthropic":   { "state": "ready", "detail": "key present" },
      "xai":         { "state": "unavailable", "detail": "no XAI_API_KEY" },
      "last_error":  { "state": "degraded", "detail": "401 from provider 2m ago" }
    },
    "embeddings":    { "state": "warming", "detail": "loading bge-small-en-v1.5" },
    "transcription": { "state": "unavailable", "detail": "faster-whisper not installed",
                       "model": "base", "compute_type": "int8" },
    "push":          { "state": "ready" },
    "geocoder":      { "state": "ready", "detail": "nominatim..." },
    "scheduler":     { "state": "ready", "detail": "last beat 12s ago" },
    "entity_rebuild":{ "state": "ready" },
    "image_analysis":{ "state": "ready" },
    "update_sidecar":{ "state": "unavailable", "detail": "autoupdate profile off" }
  }
}
```

### B3. Types

Backend Pydantic `CapState` enum + `Capability` model. Frontend `web/src/status.ts`:
```ts
export type CapState = "ready"|"warming"|"degraded"|"unavailable"|"unknown";
export interface Capability { state: CapState; detail?: string|null; last_checked?: string; model?: string; compute_type?: string; }
export interface SystemStatus { ok: boolean; brain: string; version?: string; ts: string; overall: "ready"|"degraded"|"down"; capabilities?: Record<string, Capability | Record<string, Capability>>; }
```

### B4. Caching

(1) In-process micro-cache in `snapshot()`, TTL `STATUS_TTL=3s` (like `system.py:32`). (2) `Cache-Control: no-store` + optional `ETag`→`304`. Cost: ~1 trivial query, no model/network/tokens.

---

## (c) Heartbeat hook (`web/src/heartbeat.ts`, new)

`useHeartbeat()` + shared store so multiple components share ONE poller.

```ts
type Reachability = "online"|"server-unreachable"|"browser-offline";
interface Heartbeat {
  reachability: Reachability; status: SystemStatus | null;
  overall: "ready"|"degraded"|"down"|"unknown";
  lastOkAt: number | null; stale: boolean; refreshNow: () => void;
}
```

Reachability: `!navigator.onLine` → `browser-offline` (don't fetch); fetch rejects/times out/non-2xx → `server-unreachable`; 2xx → `online` (then `overall` drives degraded).

Cadence: base **20s** when ready+visible; **15s** when degraded/warming; exponential backoff on unreachable (5→10→20→40→cap 60s, reset on success). Pause when hidden (`visibilitychange`); immediate poll + resume on visible/focus/`online`; `offline` event → `browser-offline` without fetch. AbortController **8s** timeout → `server-unreachable`. Single-flight.

Auth: poll via raw `fetch(u("/api/system/status?detail=1"),{headers:authHeaders()})` — does NOT throw on 401, NEVER calls `clearAccessKey()`. Unauthed → public skeleton (200). The only place a 401 clears the key remains `App.tsx:106` fed by `/api/auth/verify`. Documented invariant.

---

## (d) Status indicator UX

`web/src/components/StatusIndicator.tsx`, mounted in `Shell.tsx` top bar (`:243`):

| Condition | Dot | Label |
|---|---|---|
| browser-offline | gray | "Offline" |
| server-unreachable | red | "Server unreachable" |
| online + ready | green/hidden | none |
| online + degraded | amber | "Some features limited" |
| any warming | blue pulse | "Starting up…" |

Keep version-mismatch banner; **replace** the `navigator.onLine`-only offline banner with one driven by `reachability` so "server unreachable while device online" is finally distinguishable. Tapping the dot opens a detail panel (reuse `Modal.tsx`) listing each capability with dot + `detail` + `last_checked`, remediation hints, and a Refresh button. Link from `SystemPage`. New CSS vars `--status-ok/warn/down/off/warming`.

---

## (e) Systematic pre-flight gating

Add a sibling `StatusCtx` (keep auth/health decoupled); `useCapabilities()` returns typed booleans, falling back to `auth.hasLlm` before the doc loads.

| Feature | Anchor | Gate | Disabled copy |
|---|---|---|---|
| Image "Analyze with AI" | `Attachments.tsx:38` | `llmReady` | "AI analysis needs an LLM key." |
| Transcribe (audio/video) | `Attachments.tsx` | `transcriptionReady` | "Transcription unavailable: speech model not installed." / "Starting…" when warming |
| Semantic search | search page | `embeddingsReady` | warming → "starting up — keyword results only"; keep FTS |
| Chat LLM modes | Chat | `llmReady` | "Assistant needs an LLM key." keep capture/dictation |
| Note AI analysis | NotePage | `llmReady` | "AI analysis off (no LLM key)." |
| Entity rebuild / research / KB synth | Entities, research | `llmReady`(+embeddings) | "Needs an LLM key." |
| Push subscribe | Settings | `pushReady` | "Push unavailable: no VAPID key." |
| Map/address | MapPage | `geocoderReady` | "Geocoding disabled." |
| Any LLM/embeddings action while unreachable | global | `reachability` | "Server unreachable" tooltip |

Mechanism: declarative `web/src/components/Gated.tsx`:
```tsx
<Gated when={transcriptionReady} reason="Transcription model not installed">
  <button onClick={transcribe}>Transcribe</button>
</Gated>
```
Renders enabled when `when`, else disabled control + tooltip/reason (+spinner+"Starting…" when warming).

---

## (f) Richer in-the-moment error surfacing

`web/src/components/Toast.tsx` + `useToast()` (~80 lines, no dep). Replace blocking `alert()`s in Chat/NotePage/Attachments with toasts including server `detail` (`api.ts:46-54`). In `api.ts`, tag caught network errors with `kind:"network"` so callers show "Server unreachable — your change wasn't saved" and trigger `refreshNow()`. Degraded + slipped-past-gating action → catch reads heartbeat doc for a specific message. Keep SSE 90s stall watchdog; on stall toast + mark stale. Converts silent `.catch(()=>{})` into visible signals.

---

## (g) Constraint compliance (§8)

Offline-tolerant (heartbeat never mutates auth; 401≠logout) ✅ · Cross-origin (public skeleton, `u()`/`authHeaders()`, CORS `*`/bearer) ✅ · No token burn (key-presence + opportunistic last-error) ✅ · Cheap & frequent (in-memory + 3s cache + SELECT 1; 20s paused-when-hidden + backoff) ✅ · Graceful degradation (observed not forced; FTS works while warming) ✅ · No heavy deps ✅ · Security (public skeleton leaks only `{ok,brain,overall,ts}`) ✅

---

## Ordered phases

1. Backend readiness primitives: `embeddings.py`, `audio_transcription.py` (+`find_spec`), `llm._last_auth_error`, scheduler heartbeat.
2. Aggregator `system_status.py` + soft-auth router + register + tests.
3. Client `status.ts` types + `heartbeat.ts` hook/store; `StatusCtx` + `useCapabilities()`.
4. `StatusIndicator` + panel; mount in Shell; replace offline banner. `Toast`/`useToast` + `api.ts` network path.
5. `Gated.tsx` rollout (transcription first, then search, chat, push, map).
6. Polish: ETag/304, SystemPage diagnostics, copy.

## Testing strategy

**Backend:** snapshot returns all keys/valid states, no model load when cold; public vs authed depth; embeddings/transcription transitions (mock/force ImportError); LLM degraded after simulated auth error AND assert no provider HTTP call; scheduler stale; micro-cache shares snapshot.
**Frontend:** reachability matrix; backoff + reset; pause-on-hidden/resume-on-focus; AbortController timeout; heartbeat 401 does NOT clear key (spy); `Gated` disabled+reason+spinner; indicator color per state.
**Manual:** cross-origin pre-login skeleton; kill server mid-session → red, stays authed, recovers; cold boot warming→ready with keyword-only search message.

## Risks & tradeoffs (honest)

- **Polling not truly real-time** + steady tiny request volume; SSE lower-latency but heavier across CORS. Mitigated by pause-when-hidden + backoff; future: short SSE only while warming.
- **Distributed flags touch hot lazy-load paths** — bug could mislabel readiness. Mitigated by keeping flags observational + tests asserting no behavior change.
- **Three connectivity sensors** (`navigator.onLine`, heartbeat, verify) risk inconsistent UI. Mitigated by making heartbeat the single ambient source.
- **Soft-auth one-route-two-depths** is subtle; could leak detail. Mitigated by a dedicated unauthed-shape test.
- **LLM "ready" heuristic** — present-but-invalid key shows green until first use. Accepted (cost); toast covers gap.
- **Toast is net-new surface** — keep minimal, confined to replacing alerts.
- **Scheduler heartbeat writes meta every 60s** — negligible, coalesced.

### Critical files
- `server/app/services/embeddings.py`, `server/app/services/audio_transcription.py`, `server/app/main.py`, `web/src/App.tsx`, `web/src/api.ts`

---

## ~250-word summary

Build one purpose-built, well-typed, cached `GET /api/system/status` aggregating every subsystem's readiness as an explicit five-value state machine (ready/warming/degraded/unavailable/unknown), driven by a single heartbeat hook. It separates the three axes today's UI conflates: browser-offline, server-unreachable, server-up-but-degraded. Key moves: (1) observational readiness flags around the existing lazy `_get_model()` loaders — never forcing work; (2) a cheap aggregator reading config accessors + scheduler heartbeat + opportunistic LLM last-auth-error, so LLM health is "key present"+"last call failed" with zero token spend; (3) one soft-authed route returning a public skeleton pre-login and full detail when keyed — no 401, never touching offline-tolerant auth; (4) heartbeat with 20s interval, backoff when down, pause-on-hidden, 8s AbortController; (5) declarative `<Gated>` pre-flight gating from the same doc; (6) a dependency-free toast replacing silent catches and blocking alerts. Biggest weakness: polling isn't truly real-time and adds steady tiny request volume; and LLM "ready" is heuristic (invalid-but-present key shows green until first use), an accepted tradeoff to avoid burning tokens per poll.
