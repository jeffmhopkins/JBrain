# Hybrid v1 — Server/API Health Indication + Pre-flight Capability Gating

**Author:** Lead architect (synthesis) · **Repo:** /home/user/JBrain · **Date:** 2026-06-08
**Inputs:** `00-research.md`; the four v2 plans (`30-plan-{A,B,C,D}-v2.md`); the two
convergent round-2 critiques (`40-redteam2-backend.md`, `40-redteam2-frontend.md`).
**Status:** implementation-ready. Every load-bearing claim below was re-verified
against the live tree on 2026-06-08 (anchors are exact unless marked "~").

---

## 1. Executive summary + design principles

The goal (from the owner): **real-time server *and* API health in the PWA, and
any service that won't actually work should say so *before* you try to use it.**

This hybrid builds the design the two round-2 red teams converged on, taking
best-of-breed from each plan:

- **Backend:** one new **soft-auth** `GET /api/system/status` on a dedicated
  lightweight router (NOT the owner-gated `/api/system`), built from **two
  independent response builders** — a tiny public skeleton `{ok, brain, ts}`
  (allowlist-tested, leaks no more than `/api/health`+`/auth/info`) and a full
  authed capabilities document. The capabilities document is *also* folded into
  the existing `/api/auth/verify` so the PWA gets a **free initial snapshot on
  boot** (zero extra round-trip). Readiness state machines in `embeddings.py` and
  `audio_transcription.py` (one `threading.Lock` guarding both writes and the
  snapshot read; audio keys off `_model_key == want`). LLM health is
  **key-presence only** server-side (no token burn); runtime validity comes from
  the client observed-health feed. The real pre-existing **search.py bug**
  (semantic calls un-wrapped) is fixed. Public shares gate via the server-driven
  `llm_ready` landing flag. Scheduler heartbeat is **cut**.

- **Frontend:** a non-throwing `getStatus()` helper in `api.ts` (keeps
  `authHeaders` private; distinguishes `needs-auth` from `unreachable`; owns an 8s
  abort; never clears the key). A `useSyncExternalStore` **module-singleton**
  health store (not threaded through `AuthCtx` — avoids re-render storms) holding
  three-axis reachability + per-subsystem caps + observed-outcome overlays. An
  **observed-health feed** instruments `api()`/`streamChat`/`streamSSE` to
  downgrade LLM/server health from real request outcomes at zero token cost.
  Provider mounts **above the auth gate** for pre-auth reachability on KeyEntry,
  with a **carve-out** so it never wraps `/share/:token`. Adaptive cadence
  (5s warming / 20s steady, paused when hidden). Status dot + detail panel in
  `Shell.tsx`, replacing the `navigator.onLine`-only banner with three-axis
  reachability. Gating takes **Plan C's re-verified inventory wholesale** with
  single-sourced `CAP_COPY`, a copy-exhaustiveness test, and three shared
  primitives. An ~80-line dependency-free toast replaces blocking `alert()`s.

### Design principles

1. **Honest, not theatrical.** "Real-time" = adaptive poll (the *floor*) +
   observed-outcomes from real traffic (the *near-real-time layer*). We do not
   claim push; we claim "near-real-time, observed." No synthetic LLM probes.
2. **Never break offline-tolerant auth.** Only a real 401 from `/api/auth/verify`
   logs out (`App.tsx:106`). The poller is non-throwing and never touches
   `clearAccessKey` — safe *by construction*.
3. **Degrade where a non-AI fallback exists; block-and-explain where it would just
   fail.** Keyword search always works; capture always saves; E2EE chat never
   needs an LLM.
4. **Cheap & frequent.** Status reads are in-memory + at most one `SELECT 1`. No
   model load, no network, no tokens per poll.
5. **Leak nothing pre-auth** beyond what `/auth/info` already exposes.
6. **Smallest correct diff.** Two service wraps, one assembler, one router, one
   `/verify` extension; client store + helper + observed feed + indicator + gating.

---

## 2. Provenance — each major decision → source plan + red-team finding resolved

| Decision | From | Resolves |
|---|---|---|
| Soft-auth `GET /api/system/status`, dedicated router (not `/api/system`, not `chat`) | **B/D** | BE 2b ("new router right"); FE 4 (pre-auth reachability) |
| Two independent builders + exact-key-allowlist public-skeleton test | **B** | BE 2a; FE security posture |
| Fold capabilities into `/api/auth/verify` for a free boot snapshot | **A** | BE 2b ("these compose"); avoids first-paint round-trip |
| `threading.Lock` around both `_set_state` and the readiness snapshot read | **B/D** | BE 1b MUST-FIX 1 (A/C showed unguarded read/write) |
| Reject "delete the lock; it's all on the loop"; state set *inside* `_get_model` | red-team | BE 1b MUST-FIX 2 (D's analysis was wrong — warm path runs on `to_thread` worker) |
| Audio readiness keys off `_model_key == want` with `ready→warming` stale downgrade + `model`/`compute_type` echo | **A** | BE 1a (round-1 HIGH, now closed) |
| Single `llm` object with `providers:{anthropic,xai}` map (not split sub-objects) | **A/C** | BE 5 ("prefer providers map" — matches `ModelPicker`) |
| LLM health = key-presence only; validity via client observed feed | **all** | BE 5a/5b; §8 cost constraint |
| `search.py:80-92` try/except → keyword fallback (per-call + debug log) | **C** | BE 3; FE MUST-FIX 1 (HIGH — A's gating rested on a non-existent fallback) |
| Public-share `llm_ready` flag on both landings | **C** | FE 6; the only honest pre-flight for the un-gateable route |
| Scheduler heartbeat CUT | **A/C/D** | BE 4b (recommend cut) |
| `getStatus()` non-throwing helper, `authHeaders` stays private | **A** | FE 1 winner; FE MUST-FIX 8 (reject D's "export authHeaders") |
| `useSyncExternalStore` module-singleton store | **D** | FE 2/MUST-FIX 4 (only design avoiding re-render storms) |
| `applyObserved` downgrade/self-heal + `llm-ok` fast-heal | **A** | FE 2 (bounds false positives, fastest honest heal) |
| "neterr/stall AND no server byte within ~8s" before red | **B/D** | FE MUST-FIX 6 (single transient 5xx must not flip red) |
| Provider above auth gate **+ carve-out for `/share/:token`** | **B** + red-team | FE 4/MUST-FIX 3 |
| `needs-auth` as a 4th server-health state | **A** | FE 1/MUST-FIX 10 (rotated key → amber, not red) |
| Plan C inventory wholesale (incl. dropped wrong gates) | **C** | FE 5/MUST-FIX 5 (only inventory surviving spot-checks) |
| Drop OwnerChatPage llm-gate (E2EE) + Map geocoder gate (no-op) | **C** | FE 5 (B's wrong gates) |
| SearchPage `:79` toast exclusion | **C** | FE 7/MUST-FIX 7 (keystroke toast storm) |
| `ApiError.category` shared by toast + dot | **B/C** | FE 7 |
| Three-axis reachability replacing `navigator.onLine`-only banner | **B/D** | research §5/§7.3 gap |

---

## 3. Backend

### 3.1 Readiness state machines

Shared vocabulary: `unknown | warming | ready | degraded | unavailable | failed`.
- `unknown` = pre-first-observation (cold, pre-warm).
- `warming` = model loading (or, for audio, a Settings-driven reload in flight).
- `ready` = usable now.
- `unavailable` = package/feature not installed (sticky; e.g. faster-whisper missing).
- `failed` = load raised a non-import error (carries truncated `last_error`).
- `degraded` = client-only overlay (LLM key present but the last real call failed);
  never set server-side.

**CRITICAL lock discipline (BE MUST-FIX 1+2).** The warmers do
`await asyncio.to_thread(embeddings._get_model)` / `audio_transcription._get_model`
(`main.py:180,211`). Because `_set_state` lives *inside* `_get_model`, it runs on
the `to_thread` worker thread on the warm path (and on the route threadpool on a
cold first request that beats the warmer). This is a real cross-thread write.
Therefore: a single `threading.Lock` guards **both** `_set_state` and the
readiness snapshot read so the multi-field tuple can never be torn. Do **NOT**
delete the lock (rejects Plan D's reasoning). Do **NOT** take `_model_lock` inside
`readiness()` — that would let a poll block behind a multi-hundred-MB model load.

#### `server/app/services/embeddings.py` (embeddings never reload → one-shot)

Add beside `_model`/`_model_lock` (`:16-17`):

```python
import time
# LOUD CONSTRAINT: PER-PROCESS state. JBrain runs a SINGLE uvicorn worker
# (server/Dockerfile:45, no --workers). Do NOT add --workers without a shared
# readiness store or the dot will flicker between workers warming at different rates.
_state = "unknown"            # unknown | warming | ready | unavailable | failed
_last_error: str | None = None
_state_since = time.time()
_state_lock = threading.Lock()

def _set_state(s: str, err: str | None = None) -> None:
    global _state, _last_error, _state_since
    with _state_lock:                       # cross-thread: warmer runs on to_thread worker
        _state, _last_error, _state_since = s, (err and str(err)[:200]), time.time()

def readiness() -> dict:
    """O(1), no model touch, never blocks. Safe on every poll."""
    with _state_lock:                       # same lock → snapshot tuple never torn
        return {"state": _state, "last_error": _last_error, "since": _state_since}
```

Wrap `_get_model()` (`:20-30`), behavior-preserving (return/raise unchanged):

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
                except ImportError as exc:
                    _set_state("unavailable", "fastembed not installed"); raise
                except Exception as exc:                       # noqa: BLE001
                    _set_state("failed", str(exc)[:200]); raise
    return _model
```

`_warm_embeddings` (`main.py:177-202`) already calls `_get_model` → transition is
free; its swallow at `:200-201` stays (readiness captures the cause).

#### `server/app/services/audio_transcription.py` (audio RELOADS → key off `want`)

`_get_model` (`:93-110`) reloads when `_model is None or _model_key != want`,
`want = (audio_model(), audio_compute_type())` (`:97-98`, DB-meta overridable
`:46-53`). Add beside `_model`/`_model_key`/`_model_lock` (`:38-40`):

```python
import time
# LOUD CONSTRAINT: per-process state; single worker only (see embeddings.py note).
_state = "unknown"            # unknown | warming | ready | unavailable | failed
_last_error: str | None = None
_state_since = time.time()
_state_lock = threading.Lock()

def _set_state(s: str, err: str | None = None) -> None:
    global _state, _last_error, _state_since
    with _state_lock:
        _state, _last_error, _state_since = s, (err and str(err)[:200]), time.time()

def readiness() -> dict:
    """O(1) + two cheap get_meta() reads. NO model load, never blocks.
    Recomputes the desired (model, compute_type) so a Settings-driven reload reads
    as 'warming' immediately: if the cached model's key != live want, we are (about
    to be) re-downloading -> report warming, not a stale ready. We read _model_key
    WITHOUT _model_lock by design (a poll must never block behind a model load); a
    torn read costs at most one extra 'warming' tick."""
    with _state_lock:
        state, err, since = _state, _last_error, _state_since
    if state == "unavailable":
        return {"state": "unavailable", "last_error": err, "since": since,
                "model": None, "compute_type": None}
    try:
        want = (audio_model(), audio_compute_type())
    except Exception:                                          # noqa: BLE001
        want = None
    if state == "ready" and want is not None and _model_key != want:
        state = "warming"                                     # Settings swap → re-download in flight
    return {"state": state, "last_error": err, "since": since,
            "model": (want[0] if want else None),
            "compute_type": (want[1] if want else None)}
```

Wrap `_get_model()` (`:93-110`), keying transitions off `want`, set state before
re-raising `TranscriptionUnavailable` so the non-fatal boot path is preserved:

```python
def _get_model():
    global _model, _model_key
    want = (audio_model(), audio_compute_type())
    if _model is None or _model_key != want:
        with _model_lock:
            if _model is None or _model_key != want:
                _set_state("warming")
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
                except Exception as exc:                       # noqa: BLE001
                    _set_state("failed", str(exc)[:200]); raise
    return _model
```

Note (red-team 1a subtlety): `_model_key` is assigned only after a *successful*
load (`:109`), so a **failed reload** leaves the old `_model`/`_model_key` →
readiness reads `warming` (old key ≠ new want), never a false `ready`. Correct.

### 3.2 Shared capabilities assembler — `server/app/services/system_status.py` (new)

One cheap snapshot. In-memory reads + one `SELECT 1`; no model touch, no network,
no tokens. `db: SELECT 1` is the only per-poll query (catches a locked WAL /
read-only mount that process-liveness misses — red-team Area 6 recommends keeping
it).

```python
def capabilities() -> dict:
    from . import embeddings, audio_transcription, push, geocode
    from ..config import get_settings
    from ..db import get_conn
    s = get_settings()
    try:
        get_conn().execute("SELECT 1").fetchone()
        db = {"state": "ready"}
    except Exception as exc:                                   # noqa: BLE001
        db = {"state": "failed", "last_error": str(exc)[:200]}
    return {
        "llm":           {"state": "configured" if s.has_llm else "absent",
                          "providers": {"anthropic": s.has_anthropic, "xai": s.has_xai},
                          "verified": None},                   # never live-checked (cost)
        "embeddings":    embeddings.readiness(),
        "transcription": audio_transcription.readiness(),
        "push":          {"state": "ready" if push.public_key() else "absent"},
        "geocoder":      {"state": "ready" if geocode.enabled() else "absent"},
        "db":            db,
    }
```

Subsystem sources (verified): `push.public_key()` (`push.py:67`),
`geocode.enabled()` (`geocode.py:38`), `has_llm`/`has_anthropic`/`has_xai`
(`config.py`), `get_conn()` (`db.py:82`).

### 3.3 New soft-auth router — `server/app/routers/system_status.py` (new)

Two independent builders; the public path NEVER builds the full doc and trims it.
Uses `verify_key`/`_extract_key` from `auth.py` (`:58-64`, `:67-71` — both
importable, never throw). NOT on the owner-gated `/api/system` router (hard-401
would break offline tolerance + the pre-auth probe), NOT on `chat.py`.

```python
from fastapi import APIRouter, Request
from ..auth import verify_key, _extract_key
from ..config import get_settings
from ..services import system_status
from ..version import APP_VERSION
from ..services import clock          # or datetime.now(UTC).isoformat()

router = APIRouter(prefix="/api/system", tags=["system-status"])  # NO router-level CurrentUser

def _public_skeleton() -> dict:                       # builder #1 — EXACT allowlist
    return {"ok": True, "brain": get_settings().brain_name, "ts": clock.iso_now()}

@router.get("/status")
def status(request: Request):
    if not verify_key(_extract_key(request)):         # None/empty/bad → False, never throws
        return _public_skeleton()                     # liveness only; no rollup, no names
    return {**_public_skeleton(), "version": APP_VERSION,
            "capabilities": system_status.capabilities()}   # builder #2 — full (authed)
```

Register in the `main.py:244` router loop (add `system_status`). No Caddy change
(normal buffered JSON GET).

**Authed response shape:**

```json
{
  "ok": true,
  "brain": "My Brain",
  "ts": "2026-06-08T15:04:05Z",
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

**Public (unauthed/KeyEntry/stray-share-call) response:** exactly
`{ "ok": true, "brain": "My Brain", "ts": "..." }` — no `version`, no
`capabilities`, no subsystem names. Locked by an exact-key-allowlist test (§7).

### 3.4 Fold capabilities into `/api/auth/verify` — free boot snapshot

Extend the existing return (`auth_router.py:33-38`) with
`"capabilities": system_status.capabilities()`; keep every legacy field
(`ok`, `brain_name`, `version`, `has_llm`, `app_tz`, `owner_set`, `llm_keys`,
`vapid_public_key`). The initial snapshot rides on the existing boot `verify`
(`App.tsx:101`) and `connect()` — no new first-paint request. The **live poll**
hits `/api/system/status`, never `/verify` (which also runs
`people.owner_name()` + `push.public_key()` per call — keep that off the poll).

`/api/health` (`main.py:254-256`) is unchanged — public liveness probe; never
carries `last_error`.

### 3.5 search.py bug fix (HIGH — not optional) — `server/app/routers/search.py:80-92`

Verified bare: the two semantic calls at `:81` (`semantic_search`) and `:86`
(`semantic_search_attachments`) have **no try/except**, unlike every other branch
(keyword `:37-48,50-64`, keyword-entity `:69-78`, semantic-entity `:94-103`). Both
call `embed → embed_many → _get_model()`, which blocks under `_model_lock` while
warming (a multi-hundred-MB first download) or raises if fastembed is missing →
`/api/search` 500s. There is **no** server-side FTS fallback for the
note/attachment semantic path. Fix (red-team 3b refinement: per-call try + debug
log, not one bare wrap, to avoid masking a real query bug):

```python
if do_semantic and not entity_only:
    try:
        for i, r in enumerate(embeddings.semantic_search(conn, q, limit)):
            bump(f"note:{r['id']}", {...}, i)
    except Exception:                       # embeddings warming/unavailable → keyword results stand
        log.debug("semantic_search degraded to keyword", exc_info=True)
    try:
        for i, r in enumerate(embeddings.semantic_search_attachments(conn, q, limit)):
            bump(f"att:{r['attachment_id']}", {...}, i)
    except Exception:
        log.debug("semantic_search_attachments degraded to keyword", exc_info=True)
```

This is independently correct and ships regardless of the UI work. It is the
**load-bearing** fix that makes every plan's "hybrid degrades to keyword" gate
truthful. Pair with the client "force keyword while embeddings ≠ ready" gate so a
warming model isn't hammered at keystroke rate (SearchPage queries per keystroke).

### 3.6 Public-share pre-flight — `server/app/routers/share.py`

`llm_ready()` exists (`:197-199` → `llm.has_credentials()`). Add
`"llm_ready": llm_ready()` to **both** landing builders: `_guided_landing`
(`:163`) and `_research_landing` (`:261`), both reached via `share_read`
(`:108-117`). `SharePage` reads it from the unauthed `getShare`/`publicApi`
payload and renders "This assistant is temporarily unavailable — please check back
later" instead of letting the recipient start a chat that 404s at `start`/`turn`
(`_resolve_guided` already 404s on `not llm_ready()`, `:192`). One already-computed
boolean; no manifest, no auth, no token leak. (Caveat: `has_credentials()` = key
present, not valid — same cost rule; the existing 404 is the backstop.)

### 3.7 Scheduler heartbeat — CUT

Per A/C/D and both red teams (BE 4b): it detects only a *dead asyncio task*
(rare — `_scheduler_loop` swallows per-item errors and never raises out of the
`while`), not a *wedged action* (items already run in their own threads). The
existing image/audio watchdog (`main.py:93-103`) recovers the user-visible
"stuck pending" case. The observed-outcomes feed covers user-facing failures. If
ever needed: B's off-thread `to_thread(set_meta(...))` template, gated behind an
ops dashboard.

### 3.8 Multi-worker note

Single uvicorn worker confirmed (`server/Dockerfile:45`,
`CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]`, no
`--workers`). In-memory per-process readiness is fine today. LOUD comments in both
readiness modules warn that adding `--workers` without a shared store would flicker
the dot. (Anchor correction the red team flagged: it is **`server/Dockerfile:45`**,
not `Dockerfile:45`.)

### 3.9 Types (backend)

Pydantic `CapState` enum (`configured|absent|unknown|warming|ready|unavailable|failed`)
+ a `Capability` model, used to type the assembler. Optional but keeps the
contract explicit and gives FastAPI's OpenAPI a real schema.

---

## 4. Frontend

### 4.1 `getStatus()` non-throwing helper — `web/src/api.ts`

`authHeaders` stays **private** (`api.ts:34`). Export one purpose-built helper
that reuses the private `authHeaders` + `u()`, distinguishes `needs-auth` (401)
from `unreachable` (network/5xx/abort), owns an 8s `AbortController`, and **never**
calls `clearAccessKey`:

```ts
// api.ts — NEW export. Does NOT throw on 401 (unlike api()); never clears the key.
export type StatusResult =
  | { ok: true; data: any }
  | { ok: false; reason: "needs-auth" | "unreachable" };

export async function getStatus(timeoutMs = 8000): Promise<StatusResult> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);   // dead VM flips dot ≤8s
  try {
    const res = await fetch(u("/api/system/status"), {
      headers: authHeaders(), signal: ctrl.signal,
    });
    if (res.status === 401) return { ok: false, reason: "needs-auth" };
    if (!res.ok) return { ok: false, reason: "unreachable" };   // 5xx → unreachable
    return { ok: true, data: await res.json() };
  } catch {
    return { ok: false, reason: "unreachable" };          // network OR 8s abort
  } finally { clearTimeout(t); }
}
```

(The status route is soft-auth, so a poll with a valid key yields the full doc and
one with no/invalid key yields the skeleton with a 200 — `needs-auth` is then
mainly a belt for a *rotated* key once we send a bearer that no longer verifies;
on the soft-auth route an invalid bearer returns the 200 skeleton, so we also map
"got skeleton but we have a stored key" → `needs-auth` in the store, see §4.2.)

### 4.2 Health store — `web/src/health.ts` (new, `useSyncExternalStore` singleton)

A module-level singleton (NOT threaded through `AuthCtx` — FE MUST-FIX 4) so
consumers subscribe to slices and a 20s poll never re-renders the whole authed
tree. Mirrors the existing `TTS_ON_EVENT` bus pattern.

```ts
export type CapState =
  "unknown"|"warming"|"ready"|"failed"|"unavailable"|"configured"|"absent"|"degraded";
export type ServerHealth = "ok"|"unreachable"|"needs-auth"|"unknown";
export type Reachability = "online"|"server-unreachable"|"browser-offline";

export interface Capabilities {
  llm: { state: CapState; verified: boolean|null; providers: { anthropic: boolean; xai: boolean } };
  embeddings: { state: CapState; last_error: string|null; since: number };
  transcription: { state: CapState; last_error: string|null; since: number; model?: string; compute_type?: string };
  push: { state: CapState };
  geocoder: { state: CapState };
  db: { state: CapState; last_error?: string|null };
}

interface HealthModel {
  reachability: Reachability;        // three-axis (FE indicator UX)
  server: ServerHealth;
  caps?: Capabilities;
  lastOkAt: number | null;
  observed: { last5xxAt?: number; lastNetErrAt?: number; lastStallAt?: number;
              llmFailAt?: number; llmOkAt?: number };
}

// API: subscribe(cb), getSnapshot(), applySnapshot(data), report(event),
//      setServerHealth(h), setReachability(r). Exposed to React via:
export function useHealth<T>(selector: (m: HealthModel) => T): T;   // useSyncExternalStore
```

Reconciliation (declared wins; observed can only *downgrade*; observed never
*upgrades*):
1. `navigator.onLine === false` → `reachability: "browser-offline"` (dominates;
   don't fetch).
2. else recent `neterr`/`stall` **AND** no server byte within ~8s →
   `reachability: "server-unreachable"` (FE MUST-FIX 6 conjunction — a single
   transient 5xx must NOT flip red).
3. else `reachability: "online"`; `server` from the last poll
   (`ok`/`needs-auth`/`unreachable`).
4. `caps.llm`: a recent `llmFailAt` (≤60s) with no newer `llmOkAt` → render
   `degraded`; the next `llmOkAt` or poll re-asserts `configured`. Self-healing,
   zero tokens.

### 4.3 Observed-health feed — exact `api.ts` diffs

The central wrappers already witness every real outcome. Instrument them
(behavior-preserving: re-throw unchanged so `api()`'s 401-throw contract — the
`App.tsx:106` logout invariant — is untouched).

**`api()` (`api.ts:40-57`) — currently NO try/catch; ADD one:**

```ts
export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(u(path), { ...opts, headers: authHeaders(opts.headers) });
  } catch (e) {
    health.report({ kind: "neterr" });        // network failure → server suspect
    throw e;                                   // BEHAVIOR UNCHANGED
  }
  health.report({ kind: "http", status: res.status });   // <500 stamps lastOkAt; >=500 → 5xx
  if (res.status === 401) throw new ApiError("Not authenticated", 401);   // STILL THROWS → App.tsx:106 untouched
  if (!res.ok) { /* ...existing detail parsing + ApiError(category), unchanged... */ }
  if (res.status === 204) return undefined as T;
  return res.json();
}
```

**`streamChat` (`api.ts:735-740`) — the initial fetch is OUTSIDE the read-loop
`try` (which starts at `:755`); ADD a try/catch around it:**

```ts
let res: Response;
try {
  res = await fetch(u(`/api/chat/conversations/${conversationId}/message`), {
    method: "POST", headers: authHeaders(), body: JSON.stringify(body), signal: ctrl.signal,
  });
} catch (e) {
  if (!ctrl.signal.aborted) health.report({ kind: "neterr" });   // ignore user-abort
  throw e;
}
health.report({ kind: "http", status: res.status });
if (!res.body) throw new ApiError("No response stream", 500);
```

Also: the stall watchdog firing (`ctrl.abort()` at `STALL_MS=90000`, `:752-753`)
→ `health.report({ kind: "stall" })` + trigger an out-of-band poll; a
`{type:"error"}` chat event → `report({ kind: "llm-fail" })`; a clean `done` for
an LLM turn → `report({ kind: "llm-ok" })` (fast self-heal).

**`streamSSE` (`api.ts:806-810`) — same: ADD a try/catch around its initial fetch**
(outside the read-loop try at `:818`); report `neterr`/`stall` identically.

Store mapping: `http status>=500` → `last5xxAt`, server `degraded`; `http
status<500` (incl. 401/4xx) → server is answering → `lastOkAt=now`; `neterr` →
`lastNetErrAt`; `stall` → `lastStallAt`. **401 still throws unchanged.**

### 4.4 Poller — `web/src/health.ts` (`useHealthPoll`, started once above the gate)

- Cadence in a **ref** so the effect depends only on `[authed]` (no re-subscribe
  churn): 5s while any subsystem is `warming`, 20s steady; pause when hidden;
  exponential backoff on unreachable (5→10→20→40→cap 60s, reset on success);
  single-flight; 8s abort (owned by `getStatus`).
- Resume handlers mirror `Shell.tsx:49-51` exactly: `visibilitychange` + `focus`
  + **`pageshow`** (mobile/PWA pause `setInterval` while backgrounded). Immediate
  poll on `online` event too.
- On each tick: drain observed signals, call `getStatus()`, merge (observed can
  only downgrade), update the store. On `connect()` call `refreshNow()` so the
  first authed poll fires immediately.
- **Never** calls `clearAccessKey`. `needs-auth` only flips the dot.

### 4.5 Mount + carve-out — `web/src/App.tsx`

Mount the poller **above the auth gate** (so KeyEntry gets pre-auth reachability),
but **carve out `/share/:token`** (FE MUST-FIX 3) so the poller never runs on a
public share recipient's device. The cleanest shape given the verified structure
(share route at `:124`, `path="*"` element at `:125`):

- Wrap only the `path="*"` element (auth + KeyEntry + Shell tree) with the
  `StatusProvider`/poller — NOT the `/share/:token` route.
- The poller therefore runs on KeyEntry (`:127`) and Shell (`:129`) but never on
  SharePage.
- The status route is soft-auth, so even a stray pre-auth call returns the
  harmless `{ok,brain,ts}` skeleton (defense in depth).
- A minimal one-line reachability banner ("Can't reach {brain}" vs "You're
  offline") renders on KeyEntry; the full detail panel stays in Shell.
- **Do NOT touch `App.tsx:106`.** The poller is independent of the boot `/verify`;
  both may fire near-simultaneously (different routes, neither mutates the other).
- Store the `capabilities` from the boot `/verify` (`:102`) as the store's initial
  snapshot (free first paint).

### 4.6 Indicator UX — `web/src/components/Shell.tsx`

Insert `<StatusDot/>` near the brand (`Shell.tsx:240`, beside `<ReviewBell/>` at
`:243`); tap expands a detail panel (reuse `Modal.tsx` / the banner slot).

Dot color (worst-of, via `useHealth` slices):
- **green:** `reachability==="online"`, `server==="ok"`, all subsystems
  `ready`/`configured`.
- **amber:** any subsystem `warming`; OR `llm` `absent`/`degraded`; OR
  `transcription` `unavailable`; OR `push`/`geocoder` `absent`; OR
  `server==="needs-auth"` (label "Re-authenticate").
- **red:** `reachability==="server-unreachable"` OR any subsystem `failed`
  (incl. `db: failed`).
- **grey:** `unknown` (pre-first-poll) / `browser-offline`.

Copy distinguishes `unknown` ("checking…") from `warming` ("starting up — try
again shortly"). Panel lists Server (reachable / unreachable / re-authenticate,
"last ok Ns ago"), AI (configured / not configured / last request failed —
degraded), Semantic search, Transcription (ready / warming / not installed /
failed+error), Push, Geocoder, DB.

**Replace** the `navigator.onLine`-only offline banner (`Shell.tsx:261`) with the
three-axis signal: `browser-offline` → "Offline — reading cached notes only";
`server-unreachable` (browser online) → "Browser online, but JBrain server
unreachable" (the long-standing research §5/§7.3 gap). Keep the version-mismatch
banner (`:258-259`); keep `useOnline` (`hooks.ts:264`) feeding the
`browser-offline` axis.

### 4.7 Gating — primitives + Plan C inventory wholesale

#### Single-sourced copy — `web/src/capabilities.ts` (new)

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

#### Shared primitives — `web/src/components/Capability.tsx` (new)

- `useCapability(id)` reads the store → `{ ready, state, reason, providers? }`.
  Single poller folds in `ModelPicker.tsx`'s private `/verify` re-fetch
  (`:37,48-52,61`).
- `<RequiresCapability id mode="hide"|"disable"|"note">` ,
  `<CapabilityButton cap>` (sets `disabled`, `title={reason}`, `aria-disabled`;
  `warming` → spinner + "available shortly" tone; `failed`/`unavailable`/`absent`
  → danger tone), `<CapabilityNote id>`.
- Keep `auth.hasLlm` derived from the store so the one existing consumer
  (`Attachments.tsx:38`) needs no change until its row is migrated.

#### Re-verified feature → capability inventory (Plan C, wholesale)

Anchors spot-checked exact on 2026-06-08. Principle: degrade where a non-AI
fallback exists; block-and-explain otherwise.

| Feature | File / anchor | Required cap | Degrade / copy |
|---|---|---|---|
| Chat **Entry** modes (Generic/Medical/Financial) | `Chat.tsx` MODES `:55-59`, seg `:858-876`, `online` `:117,505,922` | online only | Already `!online`-gated; **no LLM** — keep usable with no key (local capture) |
| Chat **Research** mode | seg `:858-866`, used `:519,669` | `llm` + online | Disable seg cell; if selected while `llm` absent, replace safety line `:928` with `CapabilityNote("llm")` + disable Send |
| Chat **Full Brain** mode | seg `:858-866`, `:669` (`"assisted"`) | `llm` + online | Same as Research |
| Research **Deep** toggle | `Chat.tsx:939-941` | inside Research | covered transitively |
| Chat **Attach file** | `Chat.tsx:943-945` | online (local) | no LLM gate |
| Chat **Send** | `Chat.tsx:946-947` | online + (llm if chat mode) | add `(mode!=="entry" && !llmReady)` to existing `disabled` |
| Lab extraction on Medical PDF | `Chat.tsx:558-562` | `llm` | already best-effort try/catch; add `CapabilityNote("llm")` near Medical sub `:879-890` |
| Research approve/skip proposal | `Chat.tsx:713-727`, buttons `:826-827` | inside Research | transitive |
| Search **keyword** | `SearchPage.tsx:36,93-97` | none (FTS) | always available — safe default |
| Search **semantic** mode | `SearchPage.tsx:36,93-97` | `embeddings ready` | disable the mode button; if active+not-ready, **force `hybrid`** before next query so we never fire a failing semantic request per keystroke (URL may seed `mode=semantic`, `:43-44`) |
| Search **hybrid** mode | `SearchPage.tsx:36,44,93-97` | embeddings preferred | selectable while `warming`; runs keyword-only safely (§3.5 fix); "(keyword only — semantic loading)" note |
| Search **entities** mode | `SearchPage.tsx:36,93-97` | always | no gate |
| Image **Analyze with AI** | `Attachments.tsx:290` (`hasLlm && isImage`) | `llm` | migrate `hasLlm` → `useCapability("llm")`; reflects `degraded` |
| **Transcribe** (audio/video) | `Attachments.tsx:285` (ungated today) | `transcription ready` | `warming` → disabled "loading model…"; `unavailable`/`failed` → `CapabilityNote`. (Queued bg task `:133-135`, so "warn before use") |
| **Video** transcribe → vision summary | server-side `audio_transcription.py:251-252` (`has_credentials()`) | `llm` | NEW row: on video + `llm` absent, `CapabilityNote` "Transcript only — the visual summary needs an AI key." |
| Attachments help copy | `Attachments.tsx:197` (`hasLlm ? …`) | `llm` | switch conditional `hasLlm` → `useCapability("llm").ready` |
| Note **AI Analysis** ↻ | `AiAnalysisPanel.tsx:63` (`reanalyze`→`refreshNoteAnalysis:44`), mounted `NotePage.tsx:219` | `llm` | `CapabilityButton`; read-only sidecar + note when absent |
| Note **Rebuild/Draft/Regather/Guide/Redraft** | `RebuildPanel.tsx:7`, buttons `:259-292`, mounted `NotePage.tsx:372` | `llm` (+embeddings for gather quality) | gate entry control; embeddings `!ready` → "keyword-only sources" note (gather degrades safely via §3.5) |
| Note **TalkPanel** (KB notes) | `NotePage.tsx:221` (KB only) | `llm` | `CapabilityButton` on send. (Only note-embedded chat — `GuidedChat`/`ResearchChat` are NOT here) |
| **Labs** Extract/Re-analyze | `LabImportPanel.tsx:58` (`reanalyzeLabs`→`medical.py:139`→`lab_vision.py:86`) | `llm` | gate THAT button (there is no "AI import" button) |
| **Entities** identity edits | `entities.py:40-66` (`request_rebuild` after merge/split/alias), polled `EntitiesPage.tsx:66-77,96` | `llm`+`embeddings` | no standalone rebuild button → **pre-edit `CapabilityNote`**: "the rebuilt entity won't get a KB article / vector until an AI key + embeddings are available"; surface `last_error` from the status poll |
| **Map** address labels | `MapPage.tsx:212` | — | **DROPPED** (FE 5): labels are pre-resolved (`location_label`), client never geocodes; `geocoder` stays in the panel as diagnostic only |
| **Owner-assisted chat** route | `App.tsx:138` (`OwnerChatPage`) | — | **DROPPED** (FE 5): E2EE human↔human (`share.py:527-548`), opaque ciphertext, NOT LLM. No gate |
| **Push subscribe** | `Shell.tsx:15-93` (ReviewBell) | `push` | one-line note when server `push` absent |
| **Public Guided/Research share** | `SharePage.tsx:66-74` → `GuidedChat`/`ResearchChat` | `llm` (server) | server-driven (§3.6): landing `llm_ready===false` → "temporarily unavailable" instead of the chat |
| **Encrypted share chat** (`kind="chat"`) | `SharePage.tsx:77-78` → `ChatShareGuest` | none | E2EE, no gate |
| **ModelPicker** missing-key warning | `ModelPicker.tsx:37,48-52,61` | `llm.providers` | replace private `/verify` re-fetch with `useCapability("llm").providers` (folds duplicate poll) |
| Any AI/embeddings action while **unreachable** | global | `reachability` | `CapabilityButton` disabled, tooltip "Server unreachable — retrying…" |
| `/flows`, `/actions` editors | `WorkflowsPage`, `ActionsPage` | run-time only | config editors; soft per-trigger note when an action needs a missing cap (LOW) |

This table is the maintained artifact; a checklist comment at the top of
`capabilities.ts` and `AdvancedHome.tsx` points here.

### 4.8 Toast — `web/src/components/Toaster.tsx` (new, ~80 lines, no dep)

1. `useToast()` + a `Toaster` mounted once near `Shell`. Non-blocking,
   dismissible, stacked, auto-expire. Fed by the same `health.ts` bus (a 5xx
   burst → one red dot + one de-duped toast).
2. **Replace blocking `alert()`** in Chat/NotePage/Attachments with toasts
   carrying `ApiError.message`; composer rollback stays.
3. `ApiError.category` ("auth"|"network"|"unavailable"|"validation"|"server")
   inferred from status in the existing `api.ts:46-53` parse block — shared by the
   toast and the dot so they agree on classification.
4. `explainError(err, capHint?)` consults the live store + `CAP_COPY`: a
   503/feature failure that slipped a gate shows the *same* copy the gate would
   ("Semantic search is still loading…") instead of a raw `detail`.
5. Promote key silent `.catch(()=>{})` loaders to a quiet "Couldn't load X" toast
   **only when `server==="ok"`** (so a real outage shows the dot, not a storm).
6. **Keep SearchPage's `:79` catch swallowed** (FE MUST-FIX 7) — do NOT route it
   through the toast (per-keystroke storm on cold boot).
7. SSE stall watchdog untouched; on stall it also `report({kind:"stall"})`.

---

## 5. Constraint-compliance checklist (research §8)

- **Offline-tolerant auth** ✓ `getStatus`/poller never call `clearAccessKey`;
  `needs-auth` only flips the dot; observed feed leaves `api()`'s 401-throw
  intact; sole logout stays `App.tsx:106`.
- **Cross-origin** ✓ bearer via private `authHeaders()` + `u()`; CORS `*`,
  `allow_credentials` off (`main.py:232-242`); no cookies on the status path;
  independent of `expose_headers`.
- **No token burn** ✓ LLM `configured`/`absent` + `verified:null`; `degraded`
  derives from observed real traffic, never a synthetic probe; zero model calls
  per poll.
- **Cheap & frequent** ✓ status = in-memory reads + one `SELECT 1`; 5s warming /
  20s steady; paused when hidden; 8s abort; single-flight.
- **Graceful degradation** ✓ search → keyword (server fix §3.5 + UI force-keyword);
  capture still saves + re-indexes; `warming` vs `unavailable`/`failed` honest copy.
- **No new heavy deps** ✓ React + `useSyncExternalStore` + `setInterval` + a
  ~40-line bus + ~80-line toast.
- **Security** ✓ public skeleton exactly `{ok,brain,ts}` (two builders,
  allowlist-tested); detail authed-only; `last_error` `[:200]`, post-auth only;
  never echoed into `/api/health` or `/auth/info`.
- **Single-worker** ✓ LOUD comments in both readiness modules; documented in §8.

---

## 6. Ordered, dependency-aware implementation phases

Each phase is independently shippable/testable.

1. **Backend readiness + bug fix.** `embeddings.readiness()` (one-shot, locked) +
   `audio_transcription.readiness()` (key-off-`want`, locked) with LOUD
   single-worker comments; **search.py §3.5 fix** (ships standalone). Tests.
2. **Backend assembler + endpoints.** `system_status.capabilities()`; new
   soft-auth `routers/system_status.py` (two builders) registered at
   `main.py:244`; extend `/api/auth/verify` (backward-compatible). `share.py`
   `llm_ready` on both landings. curl-verifiable alone.
3. **Client core.** `getStatus()` in `api.ts`; `health.ts` store
   (`useSyncExternalStore`) + reconciliation + `useHealth`; `useHealthPoll`
   (adaptive cadence, backoff, 8s abort, resume handlers).
4. **Observed feed.** Add try/catch to `api()`, `streamChat` (`:735`), `streamSSE`
   (`:806`); wire `report()`; stall → re-poll; client-only LLM downgrade + `llm-ok`
   self-heal; `ApiError.category`.
5. **Mount + indicator.** `StatusProvider`/poller above the gate **with the
   `/share/:token` carve-out**; `StatusDot` + detail panel in `Shell.tsx`;
   three-axis banner replacing the `navigator.onLine`-only one; KeyEntry
   reachability line.
6. **Toast.** `Toaster` + `useToast`; replace `alert()`s; `explainError`; promote
   silent catches (server-ok only); keep SearchPage `:79` excluded.
7. **Gating sweep.** `capabilities.ts` (`CAP_COPY`) + `Capability.tsx`
   primitives; walk the §4.7 inventory top-to-bottom (Attachments + ModelPicker
   first to prove the primitives + fold the duplicate `/verify` poll), then Chat
   modes/Send/lab-note, SearchPage force-keyword, NotePage AI/Rebuild/Talk, Labs
   Extract, Entities note, SharePage server-driven landing, push note.

Phases 1→2 = backend, curl-verifiable. 3→5 unlock the indicator. 6→7 per-feature.

---

## 7. Testing strategy

**Backend (pytest):**
- embeddings: `unknown→warming→ready`; `ImportError`→`unavailable`; other
  exception→`failed` + `last_error`; assert `_model is None` after a `readiness()`
  call (no model load).
- audio **reload regression:** load model A → `ready`; monkeypatch `audio_model()`
  → B; assert `readiness().state == "warming"` BEFORE any reload (stale-key path),
  then `ready` after `_get_model()` reloads.
- audio `ImportError` → `unavailable`, state set before `TranscriptionUnavailable`.
- **lock:** `_set_state` and `readiness()` share `_state_lock`; readiness does NOT
  take `_model_lock` (assert a poll returns while a fake slow load holds
  `_model_lock`).
- `capabilities()` includes llm/embeddings/transcription/push/geocoder/db; `db`
  → `failed` when the connection raises; `verified is None`; `llm.complete` NOT
  called during a poll.
- `/api/system/status`: **public skeleton exact allowlist** — unauthed body keys
  `== {"ok","brain","ts"}` exactly, and `version`/`capabilities` ABSENT; authed
  body has `version` + `capabilities`; no DB write; one `SELECT 1`.
- `last_error` never appears in `/api/health` or `/auth/info`.
- `search()` returns keyword/entity hits (no hang/500) when `_get_model`
  raises/blocks (monkeypatch); per-call granularity (notes fail ≠ attachments
  skipped silently).
- guided/research landings include `llm_ready`; `start`/`turn` 404 when false.

**Frontend (vitest + RTL):**
- `getStatus`: 401→`needs-auth`; 5xx→`unreachable`; network→`unreachable`; 8s
  abort→`unreachable` (fake timers); **never** calls `clearAccessKey` (spy).
- poller: adaptive cadence flips 20s↔5s without re-subscribing (one effect setup);
  `pageshow`/`focus`/`visibilitychange`/`online` re-fire; backoff sequence + reset;
  single-flight; poll 401 does NOT log out.
- store: `useSyncExternalStore` selectors don't re-render unrelated consumers;
  observed `llm-fail`→`degraded`, then `llm-ok`/poll→`configured`; observed never
  upgrades; **5xx alone does NOT go red** — needs neterr/stall AND no-byte-8s; 401
  still throws from `api()` unchanged.
- carve-out: poller does NOT mount on `/share/:token`.
- `useCapability`/`CapabilityButton`: each state → enabled/disabled + correct
  `CAP_COPY`/`title`/`aria-disabled`; SearchPage forces keyword + fires no semantic
  request while embeddings `!ready`.
- dot color table; `unknown` vs `warming` copy; three-axis banner.
- **copy-exhaustiveness test:** every `(capId, reachable state)` has `CAP_COPY`;
  every `CAP_COPY`/inventory cap exists in the `Capabilities` type (catches drift
  both directions).
- toast: `alert()` replaced; SearchPage `:79` NOT toasted; silent-load toast only
  when server ok.

**Manual:** no faster-whisper → transcription `unavailable`, button disabled,
capture/keyword search work; edit audio model in Settings → dot goes amber
(warming) immediately; kill server → red dot ≤8s, no logout, cached pages render;
rotate key → amber "Re-authenticate" (not red); revoke LLM key mid-session, send
a chat → amber `degraded` + toast, green on next valid call; cross-origin
(Pages → VM); KeyEntry distinguishes "server down" vs "you're offline" before
login; cold boot `warming→ready` within one 5s tick; share link with no LLM key →
"temporarily unavailable" landing.

---

## 8. Residual risks & out-of-scope (honest)

**Accepted residual risks:**
1. **"Real-time" is poll + observed-outcomes, not push.** Silent subsystem death
   with no traffic lags up to 20s; any failure on real traffic surfaces instantly.
2. **LLM validity is observed, not proactive.** A revoked/over-quota key shows
   green until the first real call fails → then `degraded` + toast. Cost-driven;
   strictly better than presence-only. Affects the public-share landing flag too
   (the existing `start`/`turn` 404 is the backstop).
3. **Per-process, in-memory readiness.** Single worker today
   (`server/Dockerfile:45`); LOUD comments in both modules. Latent if anyone adds
   `--workers` without a shared store.
4. **Observed false positives.** A single transient 5xx briefly tints amber;
   bounded by short decay (5xx ~30s, llm-fail ~60s), declared-state precedence,
   and the "neterr/stall AND no-byte-8s" conjunction before red.
5. **Gating drift.** The §4.7 inventory is hand-maintained; mitigated by
   single-sourced `CAP_COPY`, the exhaustiveness test, ~1-line primitives,
   checklist comments, and the toast as a backstop for any miss.
6. **Readiness tuple micro-tear on audio `_model_key`.** `readiness()` reads
   `_model`/`_model_key` without `_model_lock` (by design — a poll must never block
   behind a model load); worst case is one extra `warming` tick. Documented inline.

**Explicitly out of scope:**
- **SSE / WebSocket push.** Dropped (Plan D's own conclusion + both red teams):
  under Caddy HTTP/2 the connection-cap argument is moot, and for a single
  self-hosted user the only pushed transition (embeddings/audio flipping ready
  ~5s after boot) is watched by nobody. The store/indicator/gating/observed feed
  are transport-agnostic, so SSE is a clean **additive future layer** if the
  deployment ever goes **multi-user or adds an ops/status dashboard** (then: a
  2-line `@sse2` Caddy block + `GET /api/system/status/stream` on the same
  soft-auth router, one new store input). **Not built now.**
- **Proactive LLM key validation** (would burn tokens).
- **Multi-worker readiness sharing** (no `--workers` today).
- **Scheduler heartbeat** (cut; low value).
- **`/flows`/`/actions` deep run-time gating** (config editors; soft note only).
