# Plan A v2 — "Minimal extension, lowest-risk diff" (red-team-hardened)
## Real-time server + API health + pre-flight capability gating

Same chassis as v1 — accurate Phase 0, the initial snapshot riding free on the
boot `/verify`, the smallest reviewable backend diff, no SSE/WS, adaptive
polling cadence — but every MEDIUM/HIGH finding from `20-redteam1-A.md` is now
fixed, and the two cheapest ideas from B/C/D are folded in (observed-outcomes
feed, anchored gating inventory, ~80-line toast). v1 is `10-plan-A-minimal.md`;
read it first if you want the rationale behind the parts that didn't change.

---

## Changes from v1 / red-team responses

Each row maps a red-team finding to the concrete fix in this doc. "Kept" =
deliberately unchanged with justification.

| # | Finding (from `20-redteam1-A.md`) | Sev | Fix in v2 |
|---|---|---|---|
| 1.1 | Audio readiness keys off a one-shot flag; a Settings-driven model reload (`audio_transcription.py:98`, `_model is None or _model_key != want`) leaves the dot green while it re-downloads. | MED | **§1b** — readiness now keys off `want`/`_model_key`. The wrap sets `warming` whenever `_model is None or _model_key != want`, captures the `want` it's loading, and only sets `ready` when the cached `_model_key` actually equals the live `want`. `readiness()` recomputes `want` cheaply (two `get_meta` reads) and reports `warming` if the cached key is stale — so a Settings change flips the dot to amber immediately, before the next transcribe blocks. |
| 1.2 | `useHealth` labels a 401 as "server unreachable"; `api()` throws `ApiError(...,401)` for any 401 (`api.ts:45`). | MED | **§3a** — the poller uses a new **raw** helper `getStatus()` that does NOT throw on 401 (it reads `res.status`). 401 → `serverHealth="needs-auth"` (distinct amber "Re-authenticate" state); network/timeout/5xx → `unreachable`. It still NEVER calls `clearAccessKey`; logout stays owned by `App.tsx:106`. `authHeaders` is module-private (`api.ts:34`), so we **export a `getStatus()` raw helper from `api.ts`** rather than leaking `authHeaders` — see §3a note. |
| 3.7 | No AbortController timeout; a dead VM leaves the poll pending until TCP timeout (30–120s), dot never flips. | MED | **§3a** — `getStatus()` takes an `AbortController` with an **8s** timeout; an abort is treated as `unreachable`. The dot flips within ≤8s of a dead box. |
| 2.2 / 1.(top-5 #1) | Only 3 subsystems; goal says "any service." | HIGH | **§1c, §2a** — broadened to **push** and **geocoder** (both pure config reads, already in `auth/verify` indirectly) and a **DB `SELECT 1`** liveness probe so "reachable" actually proves the DB is answerable, not just that the process replied. Scheduler heartbeat is **deliberately deferred** (§1d) with a crisp justification — it requires a per-iteration `set_meta` *write*, which violates the "smallest diff / no new write paths" philosophy; the observed-outcomes feed (§3c) covers the user-visible failure modes it would catch. |
| 2.3 / top-5 #5 | LLM "configured" shows green for revoked/over-quota keys; only backstop is in-the-moment error. | MED | **§3c** — adopt **Plan D's client-side observed-outcome signal** instead of B's invasive backend `_last_auth_error`. Instrument the central `api()` and `streamChat` (which already see every 5xx / network error / stall) to downgrade LLM to `degraded` on a *real* provider error, self-healing on the next success. Zero new backend state, zero extra tokens, cheaper than B's `services/llm.py` mutation. |
| 2.1 / top-5 #2 | "Real-time" is 20s polling; between-polls gap. | HIGH | **§3c** — the observed-outcomes feed closes the gap: a real 5xx/stall on actual traffic flips the dot *immediately* (no waiting for the next poll) and triggers an out-of-band `refreshNow()`. Polling becomes the *floor*, observed outcomes the *real-time* layer — without SSE. |
| 2.4 / top-5 #5 | Gating table is a hand-wave; no anchors; misses `ModelPicker.tsx`'s duplicate `/verify` poll. | MED | **§5b** — replaced with **Plan C's anchored inventory** (real line numbers) and **single-sourced copy** (`CAP_COPY`). `ModelPicker.tsx:30-66`'s private `/verify` re-fetch is folded into the shared context. |
| 2.5 | Error surfacing is the weakest section; keeps blocking `alert()`. | MED | **§6** — add a **~80-line dependency-free toast** (steal from B/C/D) and route gated-action failures + key silent catches through it. |
| 1.3 | Hook dep array `[authed, caps.embeddings.state, caps.transcription.state]` causes request churn (re-subscribe per state byte). | LOW | **§3a** — cadence held in a `ref`; effect depends only on `[authed]`. Cadence adapts without tearing down the effect/interval. |
| 1.5 / B's `unknown` | `idle` vs `warming` copy undefined for the pre-first-poll window. | LOW | **§1a/§4** — explicit `unknown` (pre-first-poll/pre-warm) state with its own copy, distinct from `warming`. |
| 1.4 | Two anchor nits: hook omits `pageshow`; ReviewBell resume pattern. | LOW | **§3a** — poller now mirrors `Shell.tsx:49-51` exactly: `visibilitychange` + `focus` + **`pageshow`** (the codebase adds `pageshow` deliberately because mobile/PWA pause `setInterval` while backgrounded — `Shell.tsx:36-39`). |
| 3.1 | Multi-worker flicker (latent). | LOW | **§7** — kept as-is (verified single-worker: `Dockerfile:45` runs `uvicorn app.main:app` with no `--workers`; `llm.py:36`). Now documented **loudly** as a hard constraint: a `LOUD CONSTRAINT` note in both readiness modules so nobody adds `--workers` without a shared store. |
| 3.3 | `last_error` could leak paths/URLs. | LOW | **§2, §7** — kept `[:200]` truncation + `CurrentUser` gating; added an explicit test that `last_error` never appears in public `/api/health` or `/auth/info`. |

**Kept from v1 (strengths the critique told me to preserve):** accurate Phase 0;
zero-extra-round-trip initial snapshot via the boot `/verify` (§2b); smallest
reviewable diff (two ~12-line service wraps + one assembler + one route); no
SSE/WS (correctly avoids the browser 6-connection cap that already bit
`streamChat`, `api.ts:728-733`); adaptive cadence; `configured`/`absent` LLM
shape with `verified:null` (no token burn).

---

## Phase 0 — Verified facts (re-checked against current code for v2)

Confirmed against code on the v2 pass:
- `/api/health` liveness-only: `server/app/main.py:254-256` → `{"ok","brain"}`.
- `/api/auth/verify` is the manifest: `auth_router.py:22-38` (also calls
  `people.owner_name(get_conn())` + `push.public_key()` → justifies a separate
  cheaper `/status`).
- Embeddings lazy, **no reload**, no readiness flag: `embeddings.py:16-30`
  (`_model`, `_model_lock`, `_get_model`; fastembed imported inside `_get_model`
  at `:25`).
- Audio lazy, **DOES reload** on Settings change: `audio_transcription.py:38-40`
  (`_model`, `_model_key`, `_model_lock`), `_get_model` `:93-110` with the
  `_model is None or _model_key != want` guard at `:98`; `want = (audio_model(),
  audio_compute_type())` from DB-`meta`-overridable accessors `:46-53`;
  `ImportError → TranscriptionUnavailable` at `:102-107`.
- Warmups fire-and-forget, errors swallowed: `_warm_embeddings` `main.py:177-202`,
  `_warm_audio` `main.py:208-215`.
- `authHeaders` is **module-private** in `api.ts:34`; `api()` `:40-57` throws
  `ApiError("Not authenticated",401)` at `:45`; `get`/`post` exported `:67-72`;
  `u()` exported `:30`; `streamChat` `:712-777` (stall watchdog `STALL_MS=90000`
  `:752`); `streamSSE` `:803`.
- `AuthCtx`/`useAuth` inline in `App.tsx:47-48`; `AuthState` `:33-45`;
  `connect()` `:77-88`; boot effect `:97-111`; offline-tolerant catch `:106`.
- Banners `Shell.tsx:258-261`; header insert point near `:240-243` (brand +
  `<ReviewBell/>`); ReviewBell resume handlers `Shell.tsx:49-51`
  (visibility/focus/**pageshow**), rationale `:36-39`; `useOnline` `hooks.ts:264-277`.
- Subsystem accessors for broadening: `push.public_key()` `push.py:67`;
  `geocode.enabled()` `geocode.py:38`; `get_conn()` `db.py:82`;
  `get_settings()` `config.py:95`; `llm.has_credentials()` `llm.py:506`.
- Single worker confirmed: `Dockerfile:45` `uvicorn app.main:app` (no
  `--workers`); `llm.py:36` comment "the single uvicorn worker."
- Only systematic gate today: Attachments `hasLlm` (`Attachments.tsx:38,197,285,290`).
- `ModelPicker.tsx:30-66` does its own `/verify` re-fetch for `llm_keys`
  (duplicate poll to fold in).

---

## Phase 1 — Backend: readiness state machines (still tiny)

Shared vocabulary across subsystems:
`unknown | warming | ready | degraded | unavailable | absent | failed`.
(`unknown` = pre-first-observation; `absent` = LLM key not configured;
`unavailable` = package/feature not installed; `failed` = load error;
`degraded` = present but a real call failed.)

### 1a. Embeddings readiness — `server/app/services/embeddings.py`

Add a module-level state next to `_model`/`_model_lock` (`:16-17`). Embeddings
**never reload**, so a one-shot transition is correct *here* (unlike audio).

```python
# --- readiness state (cheap, in-memory; O(1) on read; NO model touch) -------
# LOUD CONSTRAINT: this state is PER-PROCESS. JBrain runs a single uvicorn worker
# (Dockerfile:45, no --workers; llm.py:36). Do NOT add --workers without a shared
# readiness store, or the dot will flicker between workers warming at different rates.
import time
_state = "unknown"            # unknown | warming | ready | failed
_last_error: str | None = None
_state_since = time.time()

def _set_state(s: str, err: str | None = None) -> None:
    global _state, _last_error, _state_since
    _state, _last_error, _state_since = s, err, time.time()

def readiness() -> dict:
    """O(1), no model load. Safe on every poll."""
    return {"state": _state, "last_error": _last_error, "since": _state_since}
```

Wrap `_get_model()` (`:20-30`):

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
                except Exception as exc:           # noqa: BLE001
                    _set_state("failed", str(exc)[:200])
                    raise
    return _model
```

`_warm_embeddings` (`main.py:177-202`) already calls `_get_model`, so the
transition happens for free; its swallow at `:200-201` is preserved (readiness
captures the cause). No external key → `failed` ≈ download/disk/OOM, exactly the
signal we want.

### 1b. Transcription readiness — `server/app/services/audio_transcription.py` (BUG FIX 1.1)

Audio **reloads** when `want = (audio_model(), audio_compute_type())` changes
(`:97-98`, runtime-editable via Settings GUI). Readiness must key off
`want`/`_model_key`, NOT a one-shot flag.

Add module state near `:38-40`:

```python
# LOUD CONSTRAINT: per-process state; single worker only (see embeddings.py note).
_state = "unknown"            # unknown | warming | ready | failed | unavailable
_last_error: str | None = None
_state_since = time.time()
_loading_key: tuple[str, str] | None = None   # the `want` an in-flight load is fetching

def _set_state(s, err=None):
    global _state, _last_error, _state_since
    _state, _last_error, _state_since = s, err, time.time()

def readiness() -> dict:
    """O(1) + two cheap get_meta() reads. NO model load.
    Recomputes the desired (model, compute_type) so a Settings-driven reload is
    reflected immediately: if the cached model's key != the live `want`, we are
    (about to be) re-downloading -> report 'warming', not 'ready'."""
    if _state == "unavailable":
        return {"state": "unavailable", "last_error": _last_error, "since": _state_since}
    try:
        want = (audio_model(), audio_compute_type())
    except Exception:                              # noqa: BLE001
        want = None
    state = _state
    # Stale-key detection: ready model but the configured key changed -> warming.
    if state == "ready" and want is not None and _model_key != want:
        state = "warming"
    return {"state": state, "last_error": _last_error, "since": _state_since,
            "model": (want[0] if want else None),
            "compute_type": (want[1] if want else None)}
```

Wrap `_get_model()` (`:93-110`) so transitions key off `want`:

```python
def _get_model():
    global _model, _model_key
    want = (audio_model(), audio_compute_type())
    if _model is None or _model_key != want:
        with _model_lock:
            if _model is None or _model_key != want:
                _set_state("warming")
                global _loading_key; _loading_key = want
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    _set_state("unavailable", "faster-whisper not installed")
                    raise TranscriptionUnavailable(
                        "Audio transcription needs faster-whisper "
                        "(pip install -r requirements-audio.txt).") from exc
                try:
                    _model = WhisperModel(want[0], device="cpu", compute_type=want[1])
                    _model_key = want
                    _set_state("ready")
                except Exception as exc:           # noqa: BLE001
                    _set_state("failed", str(exc)[:200])
                    raise
    return _model
```

Now: owner edits the model in Settings → next `readiness()` poll sees
`_model_key != want` → reports `warming` → dot goes amber **before** the user's
next transcribe blocks on the multi-hundred-MB re-download. `_warm_audio`
(`main.py:208-215`) still swallows; readiness captures the cause. Set state
before re-raising `TranscriptionUnavailable` so the non-fatal boot path is
preserved.

### 1c. LLM readiness (no token burn) + broadened subsystems

No backend state machine for LLM (per §8 cost). Report `configured` (key
present) vs `absent`. The *validity* signal comes from the client observed-
outcomes feed (§3c) which downgrades to `degraded` — no backend `_last_auth_error`
mutation needed (cheaper than Plan B).

Broaden the manifest with **config-only** reads (no new state, no work):

| Subsystem | Source (verified) | state |
|---|---|---|
| `llm` | `settings.has_llm`/`has_anthropic`/`has_xai` (`config.py`) | `configured` \| `absent` (client may set `degraded`) |
| `embeddings` | `embeddings.readiness()` | `unknown`\|`warming`\|`ready`\|`failed` |
| `transcription` | `audio_transcription.readiness()` | `unknown`\|`warming`\|`ready`\|`failed`\|`unavailable` |
| `push` | `push.public_key()` (`push.py:67`) | `ready` if non-empty key else `absent` |
| `geocoder` | `geocode.enabled()` (`geocode.py:38`) | `ready` if URL configured else `absent` |
| `db` | `get_conn().execute("SELECT 1")` (`db.py:82`) | `ready` on success else `failed` (+`[:200]` error) |

The DB `SELECT 1` is the only per-poll query; it directly answers the 2.2
blind spot ("reachable proves the process answered, not that the DB is
writable" — a locked WAL / read-only mount now surfaces as `db: failed`).

### 1d. Scheduler heartbeat — deliberately DEFERRED (justification)

The critique (2.2) suggests a `set_meta("scheduler:last_beat")` heartbeat in
`_scheduler_loop`. v2 **does not** add it, on purpose:
- It requires a new **write** every iteration (60s) — the only mutating change
  the broadened set would introduce. That contradicts the "smallest, safest,
  read-only status" philosophy and adds a hot-path side effect to review.
- A wedged scheduler manifests to the *user* as "my workflow/trigger didn't
  fire," which is a background concern, not a "warn before use" pre-flight one.
  The brief is "any service that won't work should tell you *before you try to
  use it*" — the scheduler isn't a thing the user *tries to use* synchronously.
- The observed-outcomes feed (§3c) already catches the user-facing failures
  (real 5xx/stall on actual requests) without a new write path.

If a future need arises, it slots in as a 1-line read in `_capabilities()` —
explicitly noted in Risks (§8). This keeps v2 the smallest diff that still
meets the literal goal for everything a user *invokes*.

---

## Phase 2 — Backend: data shape + endpoints

### 2a. Shared assembler — `server/app/routers/auth_router.py`

```python
def _capabilities() -> dict:
    from ..services import embeddings, audio_transcription, push, geocode
    from ..db import get_conn
    s = get_settings()
    try:
        get_conn().execute("SELECT 1").fetchone()
        db = {"state": "ready"}
    except Exception as exc:                        # noqa: BLE001
        db = {"state": "failed", "last_error": str(exc)[:200]}
    return {
        "llm":           {"state": "configured" if s.has_llm else "absent",
                          "providers": {"anthropic": s.has_anthropic, "xai": s.has_xai},
                          "verified": None},         # never live-checked (cost)
        "embeddings":    embeddings.readiness(),
        "transcription": audio_transcription.readiness(),
        "push":          {"state": "ready" if push.public_key() else "absent"},
        "geocoder":      {"state": "ready" if geocode.enabled() else "absent"},
        "db":            db,
    }
```

### 2b. Extend `/api/auth/verify` (`auth_router.py:22-38`) — zero extra round-trips

Add `"capabilities": _capabilities()` to the existing return; keep every legacy
field. The **initial** snapshot rides in on the existing boot `verify`
(`App.tsx:101`) and `connect()` (`App.tsx:81`) — no new first-paint request.

### 2c. New cheap live poll — `auth_router.py` (shares `_capabilities()`)

```python
@router.get("/status", dependencies=[CurrentUser])
def status():
    # Cheap: no model touch, no network, no DB writes. One SELECT 1. Safe at 15-30s.
    return {"ok": True, "version": APP_VERSION, "capabilities": _capabilities()}
```

Why separate from `/verify`: `/verify` also runs `people.owner_name(get_conn())`
and assembles owner/onboarding fields per call. `/status` skips those → genuinely
cheap polling. Key-gated → leaks nothing pre-auth.

**Endpoint shape:**

```json
{
  "ok": true,
  "version": "1.42.0",
  "capabilities": {
    "llm":           { "state": "configured", "verified": null,
                       "providers": { "anthropic": true, "xai": false } },
    "embeddings":    { "state": "ready",   "last_error": null, "since": 1717800000.1 },
    "transcription": { "state": "warming", "last_error": null, "since": 1717800012.4,
                       "model": "base", "compute_type": "int8" },
    "push":          { "state": "ready" },
    "geocoder":      { "state": "absent" },
    "db":            { "state": "ready" }
  }
}
```

### 2d. Liveness stays put

`/api/health` unchanged — public Caddy/monitoring probe; never carries
`last_error` (3.3). Reachability for the dot uses the authed `/api/status`
(capabilities ride along).

---

## Phase 3 — Frontend: poller + observed-outcomes feed + context

### 3a. `getStatus()` raw helper + `useHealth` hook (FIXES 1.2, 1.3, 3.7, 1.4)

`authHeaders` is module-private (`api.ts:34`). Rather than export it (which would
invite ad-hoc auth'd fetches elsewhere), **export one purpose-built raw helper**
from `api.ts` that reuses the private `authHeaders` + `u()` internally and
distinguishes 401 from network/timeout/5xx:

```ts
// api.ts — NEW export. Does NOT throw on 401 (unlike api()); never clears the key.
export type StatusResult =
  | { ok: true; data: any }
  | { ok: false; reason: "needs-auth" | "unreachable" };

export async function getStatus(timeoutMs = 8000): Promise<StatusResult> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);   // FIX 3.7: dead VM flips dot <=8s
  try {
    const res = await fetch(u("/api/auth/status"), {
      headers: authHeaders(), signal: ctrl.signal,
    });
    if (res.status === 401) return { ok: false, reason: "needs-auth" };   // FIX 1.2
    if (!res.ok) return { ok: false, reason: "unreachable" };              // 5xx -> unreachable
    return { ok: true, data: await res.json() };
  } catch {
    return { ok: false, reason: "unreachable" };          // network OR 8s abort
  } finally { clearTimeout(t); }
}
```

Hook (`web/src/hooks.ts`, appended after `useOnline`). Cadence in a `ref` so the
effect depends only on `[authed]` (FIX 1.3); resume handlers mirror
`Shell.tsx:49-51` including `pageshow` (FIX 1.4):

```ts
export type CapState =
  "unknown" | "warming" | "ready" | "failed" | "unavailable" | "configured" | "absent" | "degraded";
export interface Capabilities {
  llm: { state: CapState; verified: boolean | null; providers: { anthropic: boolean; xai: boolean } };
  embeddings: { state: CapState; last_error: string | null; since: number };
  transcription: { state: CapState; last_error: string | null; since: number; model?: string; compute_type?: string };
  push: { state: CapState };
  geocoder: { state: CapState };
  db: { state: CapState; last_error?: string | null };
}
export type ServerHealth = "ok" | "unreachable" | "needs-auth" | "unknown";

export function useHealth(authed: boolean, initial?: Capabilities) {
  const [caps, setCaps] = useState<Capabilities | undefined>(initial);
  const [server, setServer] = useState<ServerHealth>(authed ? "ok" : "unknown");
  const cadence = useRef(20000);

  useEffect(() => {
    if (!authed) return;
    let alive = true; let id: number;
    const schedule = () => { clearInterval(id); id = window.setInterval(tick, cadence.current); };
    const tick = async () => {
      // OBSERVED-OUTCOMES (§3c): if real traffic just failed, reflect it instantly.
      const obs = drainObserved();
      const r = await getStatus();
      if (!alive) return;
      if (r.ok) {
        const merged = applyObserved(r.data.capabilities as Capabilities, obs);
        setCaps(merged); setServer("ok");
        const warming = merged.embeddings.state === "warming" || merged.transcription.state === "warming";
        const next = warming ? 5000 : 20000;     // adaptive, no effect teardown
        if (next !== cadence.current) { cadence.current = next; schedule(); }
      } else {
        setServer(r.reason === "needs-auth" ? "needs-auth" : "unreachable");  // FIX 1.2
        // CRITICAL: never clears the key. App.tsx:106 owns logout. needs-auth only flips the dot.
      }
    };
    tick(); schedule();
    const onVis = () => { if (document.visibilityState === "visible") tick(); };
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("focus", tick);
    window.addEventListener("pageshow", tick);     // FIX 1.4 (mobile/PWA pause setInterval)
    const off = onObserved(tick);                  // §3c: a real failure pings an out-of-band refresh
    return () => { alive = false; clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("focus", tick);
      window.removeEventListener("pageshow", tick); off(); };
  }, [authed]);                                    // FIX 1.3: stable deps, no per-state churn

  return { caps, server };
}
```

### 3b. Extend `AuthCtx` — `web/src/App.tsx`

- Add `capabilities?: Capabilities` and `serverHealth: ServerHealth` to
  `AuthState` (`:33-45`).
- In `connect()` (`:81`) and the boot effect (`:101`) store `v.capabilities` as
  the initial snapshot (free first paint).
- In `App()` call `useHealth(authed, initialCaps)`; thread `caps`/`server` into
  the `auth` object (`:114-118`).
- **Do not touch** the catch at `App.tsx:106`. `useHealth`/`getStatus` never call
  `clearAccessKey`.

### 3c. Observed-outcomes feed (STEAL FROM PLAN D — FIXES 2.1, 2.3, 3.6)

A dependency-free module-level bus (mirrors the existing `TTS_ON_EVENT` pattern)
that the central `api()` and `streamChat`/`streamSSE` feed. No SSE, no backend
change — it reuses the fact that those wrappers *already* see every outcome.

```ts
// web/src/health-observed.ts (new, ~40 lines)
type Obs = { kind: "5xx" | "neterr" | "stall" | "llm-fail" | "llm-ok"; at: number };
let pending: Obs[] = []; const subs = new Set<() => void>();
export function reportObserved(o: Omit<Obs, "at">) { pending.push({ ...o, at: Date.now() }); subs.forEach(f => f()); }
export function drainObserved(): Obs[] { const p = pending; pending = []; return p; }
export function onObserved(cb: () => void) { subs.add(cb); return () => subs.delete(cb); }
// Merge observed signals onto the polled snapshot (observed can only DOWNGRADE; server poll re-asserts truth).
export function applyObserved(caps: Capabilities, obs: Obs[]): Capabilities { /* see below */ }
```

Instrumentation (minimal, behavior-preserving — re-throw unchanged):
- `api()` (`api.ts:40-57`): after `fetch`, `if (res.status >= 500) reportObserved({kind:"5xx"})`;
  in a `catch (e)` that isn't an `ApiError`, `reportObserved({kind:"neterr"})` then re-throw.
  **401 still throws unchanged** → `App.tsx:106` untouched.
- `streamChat` (`api.ts:712-777`): the stall-abort path (`STALL_MS`, `:752-759`)
  and a `{type:"error"}` event → `reportObserved({kind:"stall"})` /
  `reportObserved({kind:"llm-fail"})`; a clean `done` for an LLM turn →
  `reportObserved({kind:"llm-ok"})`. Same for `streamSSE` (`:803`).

`applyObserved` rules (client-side LLM validity signal — cheaper than B's backend
`_last_auth_error`):
- recent `llm-fail` (≤60s) and no newer `llm-ok` → `caps.llm.state = "degraded"`
  ("AI key present but the last request failed"). The next successful turn
  (`llm-ok`) or the next poll re-asserts `configured`. **Self-healing, zero
  tokens.**
- recent `5xx`/`stall`/`neterr` (≤8s) with the poll also failing → reinforces
  `unreachable` immediately rather than waiting up to 20s (closes 2.1 + the
  gate-vs-click race 3.6).

This is the "real-time" layer: polling is the floor; a real failure on actual
traffic flips the dot **now** and triggers an out-of-band `getStatus()` via
`onObserved(tick)`.

---

## Phase 4 — Status indicator UX — `web/src/components/Shell.tsx`

Insert `<StatusDot/>` near the brand (`Shell.tsx:240`, beside `<ReviewBell/>` at
`:243`); tap expands a panel reusing the banner slot.

**Dot color (worst-of):**
- **green:** `serverHealth==="ok"` and all subsystems `ready`/`configured`.
- **amber:** any subsystem `warming`; OR `llm` `absent`/`degraded`; OR
  `transcription` `unavailable`; OR `push`/`geocoder` `absent`; OR
  `serverHealth==="needs-auth"` (label "Re-authenticate").
- **red:** `serverHealth==="unreachable"` OR any subsystem `failed` (incl.
  `db: failed`).
- **grey:** `unknown` (pre-first-poll).

Copy distinguishes the new `unknown` from `warming` (FIX 1.5): `unknown` →
"checking…"; `warming` → "starting up — try again shortly". Panel lists: Server
(reachable / unreachable / re-authenticate, "last ok Ns ago"); AI
(configured / not configured / **last request failed — degraded**); Semantic
search; Transcription (ready / warming / not installed / failed+error); Push;
Geocoder; DB. Reuse banner CSS + one `status-dot` class + the new toast styles
(§6).

**Distinguish from `useOnline` (kept):** the browser-network banner
(`Shell.tsx:261`) stays. When `navigator.onLine` is true but
`serverHealth==="unreachable"` → red dot + "Browser online, but JBrain server
unreachable" — closes the §5/§7.3 research gap.

---

## Phase 5 — Pre-flight gating (STEAL PLAN C's anchored inventory + single-sourced copy)

### 5a. Single source of copy — `web/src/capabilities.ts` (new, small)

```ts
export const CAP_COPY: Record<string, Partial<Record<CapState, string>>> = {
  llm: {
    absent:   "AI features need an API key — set LLM_API_KEY (Claude) or XAI_API_KEY (Grok) on the server.",
    degraded: "AI key present but the last request failed — it may be revoked or over quota.",
  },
  embeddings: {
    warming:     "Semantic search is still loading its local model — try again in a few seconds.",
    failed:      "Semantic search is unavailable (the embedding model failed to load).",
    unknown:     "Checking semantic search…",
  },
  transcription: {
    warming:     "Transcription is loading its model — try again shortly.",
    unavailable: "Audio/video transcription isn't installed on this server.",
    failed:      "Transcription failed to load.",
  },
  geocoder: { absent: "Address lookup is disabled (no geocoder configured)." },
  push:     { absent: "Push notifications aren't configured on this server." },
};
```

### 5b. Reusable helper — `web/src/components/CapabilityGate.tsx` (new)

`useCapable()` reads the shared context (single poller — folds in
`ModelPicker.tsx`'s private `/verify` fetch, FIX 2.4) and returns typed booleans
+ reasons from `CAP_COPY`; `<CapabilityButton>` renders disabled + `title`/
`aria-disabled` when not ready, with a "starting…" tone for `warming` vs a
danger tone for `failed`/`unavailable`/`absent`.

### 5c. EXHAUSTIVE feature → capability inventory (anchored — adapted from Plan C)

The maintained artifact. A checklist comment at the top of `capabilities.ts`
points here (drift mitigation).

| Feature | File / anchor | Required cap | Degrade / copy |
|---|---|---|---|
| Chat mode **Research/Full Brain** | `Chat.tsx:57-58,669` | `llm` + `serverUp` | Disable mode seg + Send; safety line shows `CAP_COPY.llm`. Entry mode stays available (local). |
| Chat **Send** | `Chat.tsx:946-947` | online + (llm if chat mode) | Add `mode!=="entry" && !llm` to existing `disabled`. |
| Lab extraction on PDF | `Chat.tsx:558-562` | `llm` | Already try/catch; add `CapabilityNote` under Medical sub when `absent`. |
| Search **semantic** toggle | `SearchPage.tsx:36` | `embeddings ready` | Disable toggle; tooltip `warming` vs `failed`; **keyword/FTS still works** (degrade, not block). |
| Search **hybrid** | `SearchPage.tsx:36` | embeddings preferred | Allowed while `warming` (server falls back to FTS); "(keyword only — semantic loading)" note. |
| Image **Analyze with AI** | `Attachments.tsx:290` | `llm` | Already gated on `hasLlm` → swap to `useCapable().llm`; also reflects `degraded`. |
| **Transcribe** (audio/video) | `Attachments.tsx:285` | `transcription ready` | `warming` → disabled "loading model…"; `unavailable`/`failed` → `CAP_COPY.transcription`. |
| Attachments help line | `Attachments.tsx:197` | `llm` | swap `hasLlm` → `useCapable().llm`. |
| Note **AI Analysis** ↻ | `AiAnalysisPanel.tsx:27-40` | `llm` | `CapabilityButton`; show read-only sidecar + note when `absent`. |
| **Rebuild/Draft/Regather/Guide/Redraft** | `RebuildPanel.tsx:5-9` | `llm` (+embeddings quality) | Gate entry button; embeddings `failed` → "keyword-only sources" note. |
| Talk / Guided / Research embeds | `TalkPanel.tsx`, `GuidedChat.tsx`, `ResearchChat.tsx` | `llm` | `CapabilityButton` on send/start. |
| **Labs AI import** | `AdvancedHome.tsx:23`, `LabImportPanel.tsx` | `llm` | Manual chart always; AI import gated. |
| **Map** address labels | `AdvancedHome.tsx:20`, `MapPage.tsx` | `geocoder` | Tiles/trail render regardless; "coordinates only" note when `absent`. |
| **Push subscribe** | `Shell.tsx:15-93` (ReviewBell) | `push` | One-line note when server `push` `absent`. |
| **ModelPicker** missing-key warning | `ModelPicker.tsx:30-66` | `llm.providers` | Replace its private `/verify` fetch with `useCapable().providers` (fold the duplicate poll). |
| Any AI/embeddings action while **unreachable** | global | `serverUp` | `CapabilityButton` disabled, tooltip "Server unreachable — retrying…". |

Principle (kept): **degrade where a non-AI fallback exists** (search keeps FTS;
capture still saves and re-indexes via `reindex_missing_note_chunks`,
`main.py:187`), **block + explain where it would just fail**. Backend
`llm.has_credentials()` guards + `TranscriptionUnavailable` stay (defense in
depth).

---

## Phase 6 — In-the-moment error surfacing (STEAL the ~80-line toast — FIX 2.5)

Dependency-free toast (no library):
1. `web/src/components/Toaster.tsx` + `useToast()` (~80 lines), mounted once near
   `Shell`. Non-blocking, dismissible, stacked, auto-expire.
2. **Replace blocking `alert()`** in Chat/NotePage/Attachments with toasts
   carrying `ApiError.message` (`api.ts:59-65`); composer rollback stays.
3. `explainError(err, capHint?)` consults the live capabilities + `CAP_COPY`: a
   503/feature failure that slipped past a gate shows the *same* copy the gate
   would have ("Semantic search is still loading…") instead of a raw `detail`.
4. Convert key silent `.catch(()=>{})` loaders near gated actions into a quiet
   "Couldn't load X" toast (only when `serverHealth==="ok"`, so a real outage
   shows the dot, not a toast storm).
5. The observed-outcomes bus (§3c) de-dupes: a 5xx burst shows one red dot + one
   toast, not N.
6. SSE stall watchdog (`STALL_MS`, `api.ts:752`) untouched; on stall it also
   `reportObserved({kind:"stall"})` (§3c).

---

## Phase 7 — Constraint (§8) compliance

- **Offline-tolerant auth:** `getStatus()`/`useHealth` never call
  `clearAccessKey`; `needs-auth` only flips the dot; logout stays at
  `App.tsx:106`.
- **Cross-origin:** `getStatus()` uses the private `authHeaders()` + `u()` →
  bearer + `serverBase`; body-only JSON; independent of `expose_headers`
  (`main.py:241`).
- **No token burning:** LLM `configured`/`absent`/`degraded`; `verified:null`;
  `degraded` derives from *observed real traffic*, never a synthetic probe.
- **Cheap & frequent:** `/status` = in-memory reads + one `SELECT 1`; 5s warming
  / 20s steady; paused when hidden; 8s abort.
- **Graceful degradation:** search → keyword; capture still saves; backend guards
  unchanged.
- **No new deps:** React state + `setInterval` + a ~40-line bus + ~80-line toast.
- **Security:** `/status` `CurrentUser`-gated; `last_error` `[:200]`, post-auth
  only; never echoed into public `/api/health` or `/auth/info` (tested, §9, 3.3).
- **Single-worker constraint (3.1):** `LOUD CONSTRAINT` comments in both readiness
  modules; documented in Risks.

---

## Phase 8 — Ordered implementation

1. Backend readiness: `embeddings.py` (one-shot) + `audio_transcription.py`
   (key-off-`want`, BUG FIX 1.1) with `LOUD CONSTRAINT` notes.
2. `_capabilities()` (broadened: +push/geocoder/db) + extend `/verify`
   (backward-compatible).
3. New `/api/auth/status`.
4. `getStatus()` raw helper + observed bus (`api.ts` instrumentation) +
   `useHealth` (`hooks.ts`).
5. Extend `AuthCtx` (`App.tsx`).
6. Status dot + panel (`Shell.tsx`) + toast (`Toaster.tsx`).
7. `capabilities.ts` (CAP_COPY) + `CapabilityGate`/`useCapable`; wire the §5c
   inventory (fold `ModelPicker`).
8. Error surfacing: `explainError`, replace `alert()`s, promote key silent catches.

1→2→3 = backend, curl-verifiable alone. 4→6 unlock the indicator. 7→8 per-feature.

---

## Phase 9 — Testing strategy

**Backend (pytest):**
- embeddings: `unknown→warming→ready`; exception → `failed` + `last_error`.
- audio **reload (regression for 1.1):** load model A → `ready`; monkeypatch
  `audio_model()` to return B; assert `readiness().state == "warming"` BEFORE any
  new load (the stale-key path), then `ready` after `_get_model()` reloads.
- audio `ImportError` → `unavailable`; state set before `TranscriptionUnavailable`.
- `_capabilities()` includes llm/embeddings/transcription/push/geocoder/db;
  `db` → `failed` when the connection raises; `verified is None`; `llm.complete`
  NOT called during a poll.
- `/status`: 401 without key, 200 with; no DB write; one `SELECT 1`.
- **3.3:** assert `last_error` never appears in `/api/health` or `/auth/info`.

**Frontend (vitest+RTL):**
- `getStatus`: 401 → `needs-auth`; 5xx → `unreachable`; network → `unreachable`;
  8s abort → `unreachable` (fake timers); **never** calls `clearAccessKey` (spy).
- `useHealth`: adaptive cadence flips 20s↔5s without re-subscribing (assert one
  effect setup); `pageshow`/`focus`/`visibilitychange` re-fire.
- observed feed: `llm-fail` → `llm.state==="degraded"`; subsequent `llm-ok` /
  poll → `configured`; observed never *upgrades*; 5xx+failed-poll → immediate
  `unreachable` (no 20s wait).
- `useCapable`/`CapabilityButton`: each state → enabled/disabled + correct
  `CAP_COPY`/`title`/`aria-disabled`.
- dot color table; `unknown` vs `warming` copy.

**Manual:** no faster-whisper → transcription `unavailable`, button disabled,
capture/keyword search work; **edit audio model in Settings → dot goes amber
(warming) immediately** (1.1); kill server → red dot ≤8s, no logout, cached
pages render; rotate key → amber "Re-authenticate" (NOT red unreachable, 1.2);
revoke LLM key mid-session, send a chat → dot goes amber `degraded` + toast.

---

## Phase 10 — Risks & tradeoffs (honest)

1. **"Real-time" is polling + observed-outcomes**, not push. Steady state lags
   up to 20s for *silent* subsystem death (disk fills with no traffic); but any
   failure on *actual user traffic* surfaces instantly via §3c. Honest residual
   gap vs literal "real-time" for the no-traffic case.
2. **LLM validity is observed, not proactive.** A revoked key shows green until
   the first real call fails — then `degraded` + toast. We accept this over
   token burn; it's strictly better than v1's "presence only."
3. **Per-process, in-memory readiness.** Single worker today
   (`Dockerfile:45`); flagged `LOUD` in code. Latent if someone adds `--workers`.
4. **Scheduler not covered.** Deferred by design (§1d); a wedged scheduler is
   invisible to the dot. Mitigation path documented (1-line read).
5. **Gating breadth is manual.** The §5c table is the maintained artifact +
   checklist comment; a new AI feature still needs a hand-added gate. The toast
   (§6) is the backstop for misses.
6. **Race between gate and click** — narrowed (observed feed flips state on the
   failing call + toast explains), not eliminated.
7. **DB `SELECT 1` per poll** is one trivial query every 5–20s; negligible, but
   it *is* the only per-poll DB touch (chosen over a fully zero-DB poll to close
   the 2.2 blind spot).

### Critical files
- `server/app/services/embeddings.py`
- `server/app/services/audio_transcription.py` (BUG FIX 1.1)
- `server/app/routers/auth_router.py` (`_capabilities`, `/status`)
- `web/src/api.ts` (`getStatus` export, observed instrumentation)
- `web/src/hooks.ts` (`useHealth`)
- `web/src/health-observed.ts` (new bus)
- `web/src/App.tsx` (`AuthCtx` extension)
- `web/src/components/Shell.tsx` (status dot, toast mount)
- `web/src/capabilities.ts` + `web/src/components/CapabilityGate.tsx` + `Toaster.tsx` (new)
