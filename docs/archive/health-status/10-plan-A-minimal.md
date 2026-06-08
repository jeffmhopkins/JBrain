# Plan A — "Minimal extension, lowest-risk diff"
## Real-time server + API health + pre-flight capability gating

### Core thesis
JBrain already has a de-facto capability manifest (`GET /api/auth/verify`) and a place to show banners (`Shell.tsx`). The lowest-risk path is: **(1)** add two tiny in-memory readiness state machines to the embeddings and audio services, **(2)** widen `/api/auth/verify` to include a `capabilities` block, **(3)** add one new ultra-cheap key-gated `GET /api/status` for the live poll, **(4)** extend the existing `AuthCtx` in `App.tsx` with a `useHealth()` poller, **(5)** add one status dot to the `Shell.tsx` header and a reusable `<CapabilityGate>` helper, and **(6)** standardize "this won't work" messaging. No new deps, no websockets/SSE, no new long-lived connections, no refactor.

---

## Phase 0 — Verified facts / corrections to research

Confirmed against code:
- `/api/health` is liveness-only: `server/app/main.py:254-256` returns `{"ok", "brain"}`.
- `/api/auth/verify` is the capability manifest: `server/app/routers/auth_router.py:22-38`.
- Embeddings lazy load with **no readiness flag**: `server/app/services/embeddings.py:16-30` (`_model`, `_model_lock`, `_get_model`).
- Audio lazy load with **no readiness flag**, raises `TranscriptionUnavailable`: `server/app/services/audio_transcription.py:38-40, 72-110`.
- Warmup tasks: `_warm_embeddings` `main.py:177-202`, `_warm_audio` `main.py:208-215`. Both `asyncio.create_task`, errors swallowed.
- There is **no `AuthContext.tsx` file** — the context is `AuthCtx` defined inline in `web/src/App.tsx:47`, exposed via `useAuth()` (`App.tsx:48`). State is set in `connect()` (`App.tsx:77-88`) and the boot effect (`App.tsx:97-111`). The offline-tolerant rule lives at `App.tsx:106`.
- Banners live in `Shell.tsx:258-261`; `useOnline` is `hooks.ts:264-277` (browser-only signal).
- CORS exposes only `X-Locations-*` headers (`main.py:241`).
- The only systematic gate today is Attachments via `hasLlm` (`web/src/components/Attachments.tsx:38, 197, 285, 290`).
- Backend LLM gating uses `llm.has_credentials()` in ~15 services (rebuild_engine, pipeline, research, note_analysis, image_analysis, architect, etc.) — server-side fail-closed guards we keep.

---

## Phase 1 — Backend: readiness state machines (smallest possible)

### 1a. Embeddings readiness — `server/app/services/embeddings.py`

Add a module-level state object next to the existing `_model`/`_model_lock`. State machine: `idle → warming → ready | failed`, with `last_error` and `since` timestamp.

```python
# --- readiness state (cheap, in-memory; no I/O on read) -----------------
import time
_state = "idle"            # idle | warming | ready | failed
_last_error: str | None = None
_state_since = time.time()

def _set_state(s: str, err: str | None = None) -> None:
    global _state, _last_error, _state_since
    _state, _last_error, _state_since = s, err, time.time()

def readiness() -> dict:
    """O(1), no model touch. Safe to call on every poll."""
    return {"state": _state, "last_error": _last_error, "since": _state_since}
```

Wrap `_get_model()` so the FIRST load transitions state:

```python
def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _set_state("warming")
                try:
                    from fastembed import TextEmbedding
                    from ..config import get_settings
                    _model = TextEmbedding(model_name=get_settings().embedding_model)
                    _set_state("ready")
                except Exception as exc:          # noqa: BLE001
                    _set_state("failed", str(exc)[:200])
                    raise
    return _model
```

`readiness()` does **zero** model work. Embeddings have no external key (fastembed is local), so a `failed` state almost always means a download/disk/OOM problem — exactly what we want to surface. `_warm_embeddings` already calls `_get_model`, so warmup transitions happen for free.

### 1b. Transcription readiness — `server/app/services/audio_transcription.py`

Same pattern near `_model`/`_model_lock`. Distinguish causes: package missing (`unavailable`), download/load error (`failed`), not-yet-loaded (`idle`/`warming`).

```python
_state = "idle"
_last_error: str | None = None
_state_since = time.time()

def readiness() -> dict:
    return {"state": _state, "last_error": _last_error, "since": _state_since}
```

Wrap `_get_model()`: set `warming` on entry; on `WhisperModel(...)` success set `ready`; on `ImportError` set `state="unavailable"`; on other exceptions set `failed`. Set state before re-raising `TranscriptionUnavailable`. `_warm_audio` already calls `_get_model` and swallows — readiness captures the cause without changing the swallow (non-fatal boot preserved).

### 1c. LLM readiness (no token burn)

No new state machine. We already have `settings.has_llm`, `has_anthropic`, `has_xai` (config-time) and `llm.has_credentials()`. Report **"key present / key absent"** only — explicitly NOT a live model call (§8 cost). Data shape names it `configured`, not `verified`.

---

## Phase 2 — Backend: the data shape + endpoints

### 2a. Shared assembler — `server/app/routers/auth_router.py`

```python
def _capabilities() -> dict:
    from ..services import embeddings, audio_transcription, llm
    s = get_settings()
    return {
        "llm":          {"state": "configured" if s.has_llm else "absent",
                         "providers": {"anthropic": s.has_anthropic, "xai": s.has_xai},
                         "verified": None},          # never live-checked (cost)
        "embeddings":   embeddings.readiness(),       # {state,last_error,since}
        "transcription": audio_transcription.readiness(),
    }
```

### 2b. Extend `/api/auth/verify` (`auth_router.py:22-38`)

Add `"capabilities": _capabilities()`. Keep every existing field for backward compatibility. The **initial** capability snapshot rides in on the existing boot `verify` call (`App.tsx:101`) with zero extra round-trips.

### 2c. New cheap live poll — `auth_router.py` (shares `_capabilities()`)

```python
@router.get("/status", dependencies=[CurrentUser])
def status():
    # Ultra-cheap: no DB writes, no model touch, no network. Safe at 15-30s cadence.
    return {"ok": True, "version": APP_VERSION, "capabilities": _capabilities()}
```

**Why separate from `/verify`:** `/verify` also calls `people.owner_name(get_conn())` (DB read) and `push.public_key()` per call. `/status` avoids both → genuinely cheap polling (§8). Key-gated → leaks nothing pre-auth.

**Endpoint shape (JSON):**

```json
{
  "ok": true,
  "version": "1.42.0",
  "capabilities": {
    "llm":           { "state": "configured", "verified": null,
                       "providers": { "anthropic": true, "xai": false } },
    "embeddings":    { "state": "ready",   "last_error": null, "since": 1717800000.1 },
    "transcription": { "state": "failed",  "last_error": "Could not load model: ...",
                       "since": 1717800012.4 }
  }
}
```

State enums:
- `llm.state`: `configured | absent`
- `embeddings.state`: `idle | warming | ready | failed`
- `transcription.state`: `idle | warming | ready | failed | unavailable`

### 2d. Liveness stays put

`/api/health` unchanged — public "is the box up" probe for Caddy/monitoring. Server-reachability for the dot uses `/api/status` (authed) so capabilities come in the same call; reachability == "the status fetch succeeded."

---

## Phase 3 — Frontend: poller + context extension

### 3a. Health hook — `web/src/hooks.ts` (append after `useOnline`)

```ts
export type CapState = "idle" | "warming" | "ready" | "failed" | "unavailable" | "configured" | "absent";
export interface Capabilities {
  llm: { state: CapState; verified: boolean | null; providers: { anthropic: boolean; xai: boolean } };
  embeddings: { state: CapState; last_error: string | null; since: number };
  transcription: { state: CapState; last_error: string | null; since: number };
}
export type ServerHealth = "ok" | "unreachable" | "unknown";

export function useHealth(authed: boolean, initial?: Capabilities) {
  const [caps, setCaps] = useState<Capabilities | undefined>(initial);
  const [server, setServer] = useState<ServerHealth>(authed ? "ok" : "unknown");
  useEffect(() => {
    if (!authed) return;
    let alive = true;
    const tick = async () => {
      try {
        const s = await get("/api/status");   // throws on 401/5xx/network
        if (!alive) return;
        setCaps(s.capabilities); setServer("ok");
      } catch (e: any) {
        if (!alive) return;
        // CRITICAL: never clears the key. 401 here only flips the dot; App.tsx owns logout.
        setServer("unreachable");
      }
    };
    tick();
    const warming = caps && (caps.embeddings.state === "warming" || caps.transcription.state === "warming");
    const id = setInterval(tick, warming ? 5000 : 20000);
    const onVis = () => { if (document.visibilityState === "visible") tick(); };
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("focus", tick);
    return () => { alive = false; clearInterval(id); document.removeEventListener("visibilitychange", onVis); window.removeEventListener("focus", tick); };
  }, [authed, caps?.embeddings.state, caps?.transcription.state]);
  return { caps, server };
}
```

Reuses `get()` (cross-origin/bearer handled). Polling pauses when tab hidden; `visibilitychange`/`focus` re-fire on resume (mirrors `ReviewBell` at `Shell.tsx:42-51`). Adaptive cadence: 5s while warming (real-time feel as models come up), 20s steady (cheap).

### 3b. Extend `AuthCtx` — `web/src/App.tsx`

- Add `capabilities?: Capabilities` and `serverHealth: ServerHealth` to `AuthState`.
- In `connect()` and boot effect, store `v.capabilities` as the initial snapshot.
- In `App()` body, call `useHealth(authed, initialCaps)` and thread into the `auth` object.
- **Do not touch** the catch at `App.tsx:106` — offline-tolerant logout preserved; `useHealth` never calls `clearAccessKey`.

---

## Phase 4 — Frontend: the status indicator UX

### 4a. Header dot — `web/src/components/Shell.tsx`

Add a `<StatusDot/>` near the brand (`Shell.tsx:240`) and an expandable detail row reusing the banner slot.

- **Dot color (worst-of):**
  - green: `serverHealth==="ok"` and all subsystems `ready`/`configured`.
  - amber: any subsystem `warming`, OR `llm.state==="absent"`, OR `transcription.state==="unavailable"`.
  - red: `serverHealth==="unreachable"` OR any subsystem `failed`.
  - grey: `unknown` (pre-first-poll).
- **Tap/tooltip** expands a compact panel: Server reachable/unreachable (last ok Ns ago); AI configured/not; Semantic search ready/warming/failed+error; Transcription ready/warming/not installed/failed+error.
- Reuse banner CSS; one new `status-dot` class.

### 4b. Distinguish from `useOnline`

Keep the `!online` browser-network banner. The new dot adds the missing "our server reachable + subsystems healthy" signal. When `navigator.onLine` is true but `serverHealth==="unreachable"` → red dot, "Browser online, but JBrain server unreachable" — closes the §5/§7.3 gap.

---

## Phase 5 — Systematic pre-flight gating

### 5a. Reusable helper — `web/src/components/CapabilityGate.tsx` (new)

```tsx
export function useCapable() {
  const { capabilities, hasLlm, serverHealth } = useAuth();
  return {
    llm:           hasLlm,
    search:        capabilities?.embeddings.state === "ready",
    embeddingsMsg: capMessage("Semantic search", capabilities?.embeddings),
    transcription: capabilities?.transcription.state === "ready",
    transcribeMsg: capMessage("Transcription", capabilities?.transcription),
    serverUp:      serverHealth !== "unreachable",
  };
}
```

`capMessage()` maps state→copy: `warming`→"warming up, try again in a moment", `failed`→"unavailable: <error>", `unavailable`→"not installed on this server", `absent`→"no AI key configured".

### 5b. Features gated, where, copy

| Feature | File / anchor | Required cap | Disabled copy |
|---|---|---|---|
| Search submit (semantic) | `SearchPage.tsx` | `embeddings ready` | "Semantic search warming up/unavailable — keyword results still work" (degrade) |
| Chat send | `Chat.tsx` | `llm` + `serverUp` | "No AI key configured — set one in System" / "Server unreachable" |
| Note "Analyze with AI" | NotePage analysis | `llm` | "Add an AI key to enable analysis" |
| Image "Analyze with AI" | `Attachments.tsx:290` | `llm` | already gated — swap to `useCapable().llm` |
| Transcribe (audio/video) | `Attachments.tsx:285` | `transcription` | "Transcription unavailable on this server" when `unavailable/failed` |
| Rebuild / Research / Labs AI | respective pages | `llm` | "Requires an AI key" |
| Entity rebuild | EntitiesPage | `llm` (+ embeddings) | "Requires AI key" / "Embeddings unavailable" |

Principle: **degrade where a non-AI fallback exists** (search keeps FTS; capture still saves, re-index backfills via `reindex_missing_note_chunks`, `main.py:187`), **block + explain where it would just fail**. Backend keeps `llm.has_credentials()` guards + `TranscriptionUnavailable` (defense in depth).

---

## Phase 6 — Richer in-the-moment error surfacing

No toast library (avoid new dep):
1. Promote silent catches **near gated actions** only. Where a gated call still fails (race), surface `ApiError.message` via the existing `alert()` pattern, prefixed with capability context.
2. `ApiError` already carries `detail`+`status` — reuse.
3. Server-down inline hint: disabled send buttons get `title` "Server unreachable — retrying…".
4. Keep the SSE stall watchdog untouched.

---

## Phase 7 — Constraints (§8) compliance

- **Offline-tolerant auth:** `useHealth` never calls `clearAccessKey`; logout stays at `App.tsx:106`.
- **Cross-origin:** poll uses `get()`/`u()` → bearer + `serverBase`. Body-only JSON.
- **No token-burning:** LLM `configured`/`absent`; `verified:null`.
- **Cheap & frequent:** `/api/status` zero DB/model/network; 5s/20s, paused when hidden.
- **Graceful degradation:** search → keyword; capture still saves; backend guards unchanged.
- **No new deps:** React state + setInterval; one small component.
- **Security:** `/api/status` `CurrentUser`-gated; `last_error` truncated `[:200]`, post-auth only.

---

## Phase 8 — Ordered implementation

1. Backend state machines (`embeddings.py` + `audio_transcription.py`).
2. `_capabilities()` + extend `/verify` (backward-compatible).
3. New `/api/status`.
4. `useHealth` hook + types (`hooks.ts`).
5. Extend `AuthCtx` (`App.tsx`).
6. Status dot + panel (`Shell.tsx`).
7. `CapabilityGate`/`useCapable` + wire gates.
8. Error surfacing polish.

1→2→3 backend, shippable + curl-verifiable alone. 4→5 unlock UI. 6→7 incremental per feature.

---

## Phase 9 — Testing strategy

**Backend (pytest):** readiness idle→failed/ready transitions via monkeypatched model classes; `ImportError`→`unavailable`; `/verify` includes `capabilities` and retains legacy keys; `/status` 401 w/o key, 200 with, no DB write; `capabilities.llm.verified is None` and `llm.complete` not called during poll.

**Frontend (vitest+RTL):** `useHealth` ok/unreachable + `clearAccessKey` NOT called; adaptive cadence via fake timers; `useCapable` state→enabled/message; `CapabilityGate` disabled `title`; dot color table test.

**Manual:** no faster-whisper → transcription `unavailable`, button disabled, capture/search work; kill server → red dot, no logout, cached pages render; cross-origin poll with bearer.

---

## Phase 10 — Risks & tradeoffs (honest)

1. **"Real-time" is near-real-time polling** (5–20s lag). True real-time needs SSE/WS — out of scope. Honest gap vs literal goal.
2. **LLM health is presence, not validity.** Revoked/over-quota key shows green, fails at call time. The in-the-moment error is the only backstop.
3. **Readiness is per-process, in-memory.** Multi-worker deploys flicker between workers warming at different rates. JBrain is single-process today; latent gap.
4. **Race between gate and reality** — model can die between poll and click; caught/explained, not airtight.
5. **No historical/trend health** — current state + last_error only.
6. **Gating breadth is manual** — a new AI feature won't be auto-gated.
7. **Embeddings `failed` accumulates un-embedded notes during outage**; boot backfill repairs on restart. Acceptable.

### Critical files
- `server/app/routers/auth_router.py`
- `server/app/services/embeddings.py`
- `server/app/services/audio_transcription.py`
- `web/src/App.tsx`
- `web/src/components/Shell.tsx`
