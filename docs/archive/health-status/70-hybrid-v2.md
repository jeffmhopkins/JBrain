# Hybrid v2 — Server/API Health Indication + Pre-flight Capability Gating

**Author:** Lead architect (synthesis) · **Repo:** /home/user/JBrain · **Date:** 2026-06-08
**Inputs:** `00-research.md`; `50-hybrid-v1.md`; the round-3 red team (`60-redteam3-hybrid.md`).
**Status:** implementation-ready. This is a COMPLETE standalone plan, not a diff.
Every load-bearing claim below was re-verified against the live tree on 2026-06-08
(anchors are exact unless marked "~"). The six round-3 blockers are fixed and each
fix was re-checked against real code (see §0).

---

## 0. Round-3 fixes applied (finding → verified fix)

| # | Round-3 finding | Severity | Fix in v2 | Verified against |
|---|---|---|---|---|
| H1 | `clock.iso_now()` does not exist → `/status` AND folded `/verify` 500 | HIGH | Use `clock.now_utc().isoformat()` everywhere the public skeleton builds its `ts`. `now_utc()` returns an aware UTC datetime; `.isoformat()` yields `…+00:00`. No new helper added. §3.3, §3.4. | `clock.py:51` `now_utc()` exists; `iso_now` / `app_tz_name`/`now_local`/`now_prompt` are the only public time fns — no `iso_now`. |
| H2 | Two disagreeing "LLM ready" predicates (config presence vs active-provider creds) → green dot but dead assistant on provider-mismatch config | HIGH | ONE authoritative boolean: `llm.has_credentials()` (active provider) drives `llm.state` in the capabilities doc, the dot, every feature gate, and the share landing. The `providers:{anthropic,xai}` map is kept as **informational only** (ModelPicker per-provider hint). §3.2, §4.2, §4.6, §4.7. | `llm.has_credentials():506` → `get_provider().has_credentials()`; provider creds: Anthropic `:142-143` (`llm_api_key`), xAI `:277-278` (`self._key()`). Config props `has_anthropic:71`, `has_xai:75`, `has_llm:80` are presence-only — the wrong predicate. Share already uses the right one: `share.py llm_ready():197-199`. |
| M1 | "One store" never reconciled; `connect()` (`:81`) seeding missed; two envelopes, no normalizer | MED | ONE adapter `ingestVerify(data)` maps **both** envelopes via the **same** `data.capabilities` sub-shape (verify and status now return an identical `capabilities` object). Seed from BOTH the boot effect (`App.tsx:102`) AND `connect()` (`App.tsx:81`) by calling `health.refreshNow()` / `health.ingestVerify(v)` in both. §3.4, §4.2, §4.5. | `App.tsx connect():77-88` calls `get("/api/auth/verify")` at `:81`; boot effect calls the same at `:101-102`. Two independent call sites confirmed. |
| M2 | Stall watchdog can't tell a 90s stall from a user-initiated abort → false `stall` reports on "leave the chat" | MED | Add an explicit `let stalled = false;` set inside the watchdog timeout callback (`() => { stalled = true; ctrl.abort(); }`). Report `stall` ONLY when `stalled` is true; an aborted read that is NOT `stalled` is a benign user cancel → no report. Applied to BOTH `streamChat` and `streamSSE`. §4.3. | `streamChat arm():753` = `setTimeout(() => ctrl.abort(), STALL_MS)` — no flag; catch `:759` is shared by stall AND user-abort (`signal` wired `:731-733`). `streamSSE arm():816`, catch `:821`, user-abort `abort():837` — identical ambiguity. |
| M3 | `needs-auth` asserted in §4.1 + §4.2 but implemented in neither; literal 401 ~never fires on a soft-auth route | MED | KEEP `needs-auth`, implement it concretely as **reconciliation rule 0**: `getStatus()` returns `{ok:true,data}` where `data.capabilities === undefined` (a 200 **skeleton**) AND `getAccessKey()` is non-null → store sets `server:"needs-auth"`. Detection is **skeleton-vs-stored-key**, never `status===401`. §4.1, §4.2, §4.6. | Soft-auth route returns the 200 skeleton on a bad/rotated bearer (§3.3 `verify_key` returns False, never throws — `auth.py:58-64`); `_extract_key` never throws (`:67-71`). So a literal 401 is impossible in normal operation; skeleton detection is the only viable rule. |
| M4 | Pure `semantic` mode returns `[]` during warmup even WITH the server keyword fallback (fallback only helps hybrid) | MED | The client force/redirects `semantic → hybrid` when embeddings are not `ready`, **applied ON MOUNT** (the URL can seed `mode=semantic`), and disables the `semantic` mode button until ready. This is a HARD dependency of Phase 7, not optional defense-in-depth. §3.5, §4.7. | `search.py:80-92` semantic note/attachment calls are BARE (no try/except) — every other branch is wrapped (`:37-78,94-103`). The §3.5 fix wraps them, but a pure-`semantic` query then collects zero results → `[]` → "No results" at `SearchPage.tsx:101`. `SearchPage.tsx:43-44` seeds `mode` from the URL; query effect `:71-82` sends `&mode=${mode}` per keystroke. |

**Also carried (round-3 SHOULD-FIX / LOW):**
- **M5** (LOW-MED): scope the LLM observed-health claim — `streamChat` AND `streamSSE` (rebuild/research) both feed `llm-fail`/`llm-ok` on `{type:"error"}` / clean `done`. §4.3. (`streamSSE` yields `{type:"error",message}` `:797` and `{type:"done"}` `:795`.)
- **L1**: a one-line comment in `system_status.py` that it deliberately shares the `/api/system` prefix with a *different* (soft) auth posture than the owner-gated `system.py:27`. §3.3.
- **L2**: documented that the folded `/verify` now does one `SELECT 1` per call (no longer DB-touch-free). §3.4.
- **L4**: KeyEntry reachability slot anchored. §4.5.
- **L5**: provenance table file paths qualified `routers/` vs `services/`. §2.

**Re-verified as CORRECT by round 3 and preserved unchanged:** search.py bug location + per-call try/except fix; offline-auth invariant (`App.tsx:106`); lock discipline (`_set_state` inside `_get_model` on the `to_thread` worker, single `threading.Lock`); audio reload keying off `want`; CORS/cross-origin posture (`main.py:232-242`); single-worker (`server/Dockerfile:45`); public-skeleton two-builder allowlist security; `useSyncExternalStore` singleton; zero token burn.

---

## 1. Executive summary + design principles

The goal (from the owner): **real-time server *and* API health in the PWA, and
any service that won't actually work should say so *before* you try to use it.**

This hybrid takes best-of-breed from the four v2 plans as locked by round 2, and
applies the six round-3 corrections above.

- **Backend:** one new **soft-auth** `GET /api/system/status` on a dedicated
  lightweight router (NOT the owner-gated `/api/system`), built from **two
  independent response builders** — a tiny public skeleton `{ok, brain, ts}`
  (allowlist-tested, leaks no more than `/api/health`+`/auth/info`) and a full
  authed capabilities document. The capabilities document is *also* folded into
  the existing `/api/auth/verify` so the PWA gets a **free initial snapshot on
  boot** (zero extra round-trip), using the **same `capabilities` sub-shape** so a
  single client adapter normalizes both envelopes. Readiness state machines in
  `embeddings.py` and `audio_transcription.py` (one `threading.Lock` guarding both
  writes and the snapshot read; audio keys off `_model_key == want`). **LLM health
  is the single `llm.has_credentials()` active-provider predicate** server-side
  (no token burn); runtime validity comes from the client observed-health feed.
  The real pre-existing **search.py bug** (semantic calls un-wrapped) is fixed.
  Public shares gate via the server-driven `llm_ready` landing flag (already the
  same `has_credentials()` predicate). Scheduler heartbeat is **cut**.

- **Frontend:** a non-throwing `getStatus()` helper in `api.ts` (keeps
  `authHeaders` private; returns `unreachable` on network/5xx/abort; the
  skeleton-vs-stored-key `needs-auth` rule lives in the store, not the helper;
  owns an 8s abort; never clears the key). A `useSyncExternalStore`
  **module-singleton** health store (not threaded through `AuthCtx` — avoids
  re-render storms) holding three-axis reachability + per-subsystem caps +
  observed-outcome overlays, with **one envelope adapter** seeded from BOTH boot
  and `connect()`. An **observed-health feed** instruments
  `api()`/`streamChat`/`streamSSE` to downgrade LLM/server health from real
  request outcomes at zero token cost, distinguishing a real **stall** from a
  **user abort** via an explicit flag. Provider mounts **above the auth gate** for
  pre-auth reachability on KeyEntry, with a **carve-out** so it never wraps
  `/share/:token`. Adaptive cadence (5s warming / 20s steady, paused when hidden).
  Status dot + detail panel in `Shell.tsx`, replacing the `navigator.onLine`-only
  banner with three-axis reachability. Gating takes Plan C's re-verified inventory
  wholesale with single-sourced `CAP_COPY`, a copy-exhaustiveness test, and three
  shared primitives — including the **on-mount semantic→hybrid force**. An ~80-line
  dependency-free toast replaces blocking `alert()`s.

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
6. **One predicate per concept.** "Is the assistant usable" = `has_credentials()`,
   everywhere. The per-provider presence map is informational only.
7. **Smallest correct diff.** Two service wraps, one assembler, one router, one
   `/verify` extension; client store + helper + observed feed + indicator + gating.

---

## 2. Provenance — each major decision → source + finding resolved

| Decision | From | Resolves |
|---|---|---|
| Soft-auth `GET /api/system/status`, dedicated router (not `/api/system` owner-gated, not `chat`) | **B/D** | BE 2b; FE 4 (pre-auth reachability) |
| Two independent builders + exact-key-allowlist public-skeleton test | **B** | BE 2a; FE security posture |
| Fold capabilities into `/api/auth/verify` for a free boot snapshot | **A** | BE 2b; avoids first-paint round-trip |
| `threading.Lock` around both `_set_state` and the readiness snapshot read | **B/D** | BE 1b MUST-FIX 1 |
| State set *inside* `_get_model` (warm path runs on `to_thread` worker) | red-team | BE 1b MUST-FIX 2 |
| Audio readiness keys off `_model_key == want` + `ready→warming` stale downgrade + `model`/`compute_type` echo | **A** | BE 1a |
| **`llm.has_credentials()` (active provider) as the ONE authoritative LLM-ready predicate** for dot/gates/share; `providers:{anthropic,xai}` map informational only | **A/C** + **redteam3 H2** | BE 5; **R3-H2** (dot-vs-share contradiction) |
| LLM health = `has_credentials()` presence only; validity via client observed feed | **all** | BE 5a/5b; §9 cost constraint |
| `services/search.py` semantic try/except → keyword fallback (per-call + debug log) | **C** | BE 3; FE MUST-FIX 1 |
| Public-share `llm_ready` flag on both landings (`routers/share.py`) | **C** | FE 6 |
| Scheduler heartbeat CUT | **A/C/D** | BE 4b |
| `getStatus()` non-throwing helper, `authHeaders` stays private | **A** | FE 1; FE MUST-FIX 8 |
| `useSyncExternalStore` module-singleton store | **D** | FE 2/MUST-FIX 4 |
| **ONE envelope adapter `ingestVerify`; seed from boot (`App.tsx:102`) AND `connect()` (`App.tsx:81`)** | redteam3 M1 | **R3-M1** (unreconciled store, missed connect path) |
| `applyObserved` downgrade/self-heal + `llm-ok` fast-heal | **A** | FE 2 |
| "neterr/stall AND no server byte within ~8s" before red | **B/D** | FE MUST-FIX 6 |
| **Explicit `stalled` flag in `streamChat`/`streamSSE` watchdog; report `stall` only when stalled** | redteam3 M2 | **R3-M2** (false stall on user abort) |
| Provider above auth gate **+ carve-out for `/share/:token`** | **B** + red-team | FE 4/MUST-FIX 3 |
| **`needs-auth` via skeleton-vs-stored-key reconciliation rule 0** (NOT `status===401`) | **A** + redteam3 M3 | FE 1/MUST-FIX 10; **R3-M3** (un-implemented contract) |
| Plan C inventory wholesale (incl. dropped wrong gates) | **C** | FE 5/MUST-FIX 5 |
| Drop `OwnerChatPage` (`App.tsx:138`, E2EE) llm-gate + Map geocoder gate (no-op) | **C** | FE 5 |
| **SearchPage on-mount `semantic→hybrid` force while embeddings ≠ ready** (hard requirement) | **C** + redteam3 M4 | FE 7/**R3-M4** (pure semantic empty during warmup) |
| `SearchPage.tsx:79` toast exclusion (keystroke storm) | **C** | FE 7/MUST-FIX 7 |
| `ApiError.category` shared by toast + dot | **B/C** | FE 7 |
| Three-axis reachability replacing `navigator.onLine`-only banner | **B/D** | research §5/§7.3 gap |
| Comment the deliberate shared `/api/system` prefix split-auth posture | redteam3 L1 | **R3-L1** |
| Note folded `/verify` now does one `SELECT 1` | redteam3 L2 | **R3-L2** |

(Function homes confirmed: `llm_ready`/`_resolve_guided`/`_guided_landing`/`_research_landing`/`share_read` live in **`server/app/routers/share.py`**, not `services/share.py`. `has_anthropic`/`has_xai`/`has_llm` are properties in **`server/app/config.py`**. `has_credentials` is in **`server/app/services/llm.py`**.)

---

## 3. Backend

### 3.1 Readiness state machines

Shared vocabulary: `unknown | warming | ready | degraded | unavailable | failed`.
- `unknown` = pre-first-observation (cold, pre-warm).
- `warming` = model loading (or, for audio, a Settings-driven reload in flight).
- `ready` = usable now.
- `unavailable` = package/feature not installed (sticky; e.g. faster-whisper missing).
- `failed` = load raised a non-import error (carries truncated `last_error`).
- `degraded` = client-only overlay (LLM creds present but the last real call failed);
  never set server-side.

**CRITICAL lock discipline (BE MUST-FIX 1+2).** The warmers do
`await asyncio.to_thread(embeddings._get_model)` / `audio_transcription._get_model`
(`main.py:180,211`). Because `_set_state` lives *inside* `_get_model`, it runs on
the `to_thread` worker thread on the warm path (and on the route threadpool on a
cold first request that beats the warmer). This is a real cross-thread write.
Therefore: a single `threading.Lock` guards **both** `_set_state` and the
readiness snapshot read so the multi-field tuple can never be torn. Do **NOT**
delete the lock. Do **NOT** take `_model_lock` inside `readiness()` — that would
let a poll block behind a multi-hundred-MB model load.

#### `server/app/services/embeddings.py` (embeddings never reload → one-shot)

Add beside `_model`/`_model_lock` (`:16-17`); `import threading` already present (`:7`):

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
                except ImportError:
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
`:46-53`). Add beside `_model`/`_model_key`/`_model_lock` (`:38-40`);
`import threading` already present (`:21`):

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

Note: `_model_key` is assigned only after a *successful* load (`:109`), so a
**failed reload** leaves the old `_model`/`_model_key` → readiness reads `warming`
(old key ≠ new want), never a false `ready`. Correct.

### 3.2 Shared capabilities assembler — `server/app/services/system_status.py` (new)

One cheap snapshot. In-memory reads + one `SELECT 1`; no model touch, no network,
no tokens. `db: SELECT 1` is the only per-poll query (catches a locked WAL /
read-only mount that process-liveness misses).

**LLM-ready is the single authoritative predicate `llm.has_credentials()`**
(R3-H2). `state` is `"ready"` when the **active provider** has credentials, else
`"absent"`. The `providers` map is **informational only** — it tells ModelPicker
which per-provider keys exist, but does NOT define usability.

```python
def capabilities() -> dict:
    from . import embeddings, audio_transcription, push, geocode, llm
    from ..config import get_settings
    from ..db import get_conn
    s = get_settings()
    try:
        get_conn().execute("SELECT 1").fetchone()
        db = {"state": "ready"}
    except Exception as exc:                                   # noqa: BLE001
        db = {"state": "failed", "last_error": str(exc)[:200]}
    return {
        # AUTHORITATIVE: active-provider credentials (same predicate as share llm_ready()
        # and every feature gate). NOT s.has_anthropic/has_xai (presence != usable).
        "llm": {
            "state": "ready" if llm.has_credentials() else "absent",
            "verified": None,                                  # never live-checked (cost)
            # INFORMATIONAL ONLY — per-provider key presence for ModelPicker's hint.
            # Does NOT define usability; the active provider may differ.
            "providers": {"anthropic": s.has_anthropic, "xai": s.has_xai},
        },
        "embeddings":    embeddings.readiness(),
        "transcription": audio_transcription.readiness(),
        "push":          {"state": "ready" if push.public_key() else "absent"},
        "geocoder":      {"state": "ready" if geocode.enabled() else "absent"},
        "db":            db,
    }
```

Subsystem sources (verified): `llm.has_credentials()` (`llm.py:506` →
`get_provider().has_credentials()`), `push.public_key()` (`push.py:67`),
`geocode.enabled()` (`geocode.py:38`), `has_anthropic`/`has_xai` (`config.py:71,75`),
`get_conn()` (`db.py:82`).

**Why `has_credentials()` and not the config flags (R3-H2):** with
`LLM_PROVIDER=xai` and only `LLM_API_KEY` (Claude) set, `has_anthropic` is true but
`get_provider()` returns the xAI provider whose `has_credentials()` (`llm.py:277-278`,
`bool(self._key())`) is false. v1's config-flag version would have shown the dot
**green** while the assistant 404s and the share landing (correctly) says
"unavailable." v2 makes the dot agree with reality.

### 3.3 New soft-auth router — `server/app/routers/system_status.py` (new)

Two independent builders; the public path NEVER builds the full doc and trims it.
Uses `verify_key`/`_extract_key` from `auth.py` (`:58-64`, `:67-71` — both
importable, never throw). **`now_utc().isoformat()` for `ts` (R3-H1) — there is no
`clock.iso_now()`.**

```python
from fastapi import APIRouter, Request
from ..auth import verify_key, _extract_key
from ..config import get_settings
from ..services import system_status, clock
from ..version import APP_VERSION

# NOTE: this prefix is DELIBERATELY shared with the owner-gated routers/system.py
# (system.py:27), but with a DIFFERENT (soft) auth posture: no router-level
# CurrentUser dependency. The two never collide (this router only adds /status,
# which system.py does not define). Registration order is irrelevant (distinct
# paths). Keep this router soft-auth so a poll with no/invalid key still answers.
router = APIRouter(prefix="/api/system", tags=["system-status"])

def _public_skeleton() -> dict:                       # builder #1 — EXACT allowlist
    # R3-H1: clock.now_utc() returns an aware UTC datetime; .isoformat() -> "...+00:00".
    # There is NO clock.iso_now().
    return {"ok": True, "brain": get_settings().brain_name, "ts": clock.now_utc().isoformat()}

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
  "ts": "2026-06-08T15:04:05.123456+00:00",
  "version": "1.42.0",
  "capabilities": {
    "llm":           { "state": "ready", "verified": null,
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

(`llm.state` is `"ready"`/`"absent"` from `has_credentials()`. On an absent/no-creds
config it is `"absent"`.)

**Public (unauthed/KeyEntry/stray-share-call) response:** exactly
`{ "ok": true, "brain": "My Brain", "ts": "..." }` — no `version`, no
`capabilities`, no subsystem names. Locked by an exact-key-allowlist test (§8).

### 3.4 Fold capabilities into `/api/auth/verify` — free boot snapshot

Extend the existing return (`auth_router.py:33-38`) with
`"capabilities": system_status.capabilities()`; keep every legacy field
(`ok`, `brain_name`, `version`, `has_llm`, `app_tz`, `owner_set`, `llm_keys`,
`vapid_public_key`). The `capabilities` object is **byte-identical in shape** to
`/api/system/status`'s `capabilities` — so the client's single adapter (§4.2)
normalizes both with no per-envelope branching. The initial snapshot rides on the
existing boot `verify` (`App.tsx:101-102`) AND `connect()` (`App.tsx:81`) — no new
first-paint request. The **live poll** hits `/api/system/status`, never `/verify`
(which also runs `people.owner_name()` + `push.public_key()` per call — keep that
off the poll).

**(R3-L2)** Because `system_status.capabilities()` runs one `SELECT 1`, the folded
`/verify` is **no longer DB-touch-free**. This is acceptable (one trivial indexed
read; `/verify` is low-frequency — boot + manual login), but is stated here so an
implementer doesn't assume `/verify` stays read-only.

`/api/health` (`main.py:254-256`) is unchanged — public liveness probe; never
carries `last_error`.

### 3.5 search.py bug fix (HIGH — not optional) — `server/app/routers/search.py:80-92`

Verified bare: the two semantic calls at `:81` (`semantic_search`) and `:86`
(`semantic_search_attachments`) have **no try/except**, unlike every other branch
(keyword `:37-48,50-64`, keyword-entity `:69-78`, semantic-entity `:94-103`). Both
call `embed → embed_many → _get_model()`, which blocks under `_model_lock` while
warming or raises if fastembed is missing → `/api/search` 500s. Fix (per-call try +
debug log, not one bare wrap, to avoid masking a real query bug):

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

**(R3-M4) — scope limit, made explicit.** This fix makes **hybrid** degrade
gracefully (keyword hits are already `bump`ed before the semantic block). It does
**NOT** save **pure `semantic` mode**: a `mode=semantic` query enters the
`do_semantic and not entity_only` block only, collects zero results when both calls
raise, and `search()` returns `[]` → the UI shows "No results"
(`SearchPage.tsx:101`). Therefore the client **must** force `semantic → hybrid`
while embeddings are not `ready` (§4.7), and that force is a HARD dependency of
Phase 7 (not optional). The server fix and the client force are complementary; both
ship.

This server fix is independently correct and ships regardless of the UI work.

### 3.6 Public-share pre-flight — `server/app/routers/share.py`

`llm_ready()` exists (`:197-199` → `llm.has_credentials()`) — **already the
authoritative predicate v2 adopts everywhere (R3-H2 alignment).** Add
`"llm_ready": llm_ready()` to **both** landing builders: `_guided_landing`
(`:163`) and `_research_landing` (`:261`), both reached via `share_read`
(`:108-117`). `SharePage` reads it from the unauthed `getShare`/`publicApi`
payload and renders "This assistant is temporarily unavailable — please check back
later" instead of letting the recipient start a chat that 404s at `start`/`turn`
(`_resolve_guided` already 404s on `not llm_ready()`, `:192`). One already-computed
boolean; no manifest, no auth, no token leak. (Caveat: `has_credentials()` = key
present for the active provider, not proven valid — same cost rule; the existing
404 is the backstop.) **Because the dot now uses the same predicate, the dot and
the share landing can no longer contradict each other.**

### 3.7 Scheduler heartbeat — CUT

Per A/C/D and both red teams (BE 4b): it detects only a *dead asyncio task*
(rare — `_scheduler_loop` swallows per-item errors and never raises out of the
`while`), not a *wedged action* (items already run in their own threads). The
existing image/audio watchdog (`main.py:93-103`) recovers the user-visible
"stuck pending" case. The observed-outcomes feed covers user-facing failures.

### 3.8 Multi-worker note

Single uvicorn worker confirmed (`server/Dockerfile:45`,
`CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]`, no
`--workers`). In-memory per-process readiness is fine today. LOUD comments in both
readiness modules warn that adding `--workers` without a shared store would flicker
the dot.

### 3.9 Types (backend) — DEFERRED

Pydantic `CapState` enum + `Capability` model is **deferred** (round-3 scope check):
for a single-user self-hosted app with a hand-maintained TS `Capabilities`
interface, duplicating the contract in Pydantic adds surface without a consumer
(the OpenAPI schema isn't consumed). Revisit only if OpenAPI codegen is adopted.

---

## 4. Frontend

### 4.1 `getStatus()` non-throwing helper — `web/src/api.ts`

`authHeaders` stays **private** (`api.ts:34`). Export one purpose-built helper
that reuses the private `authHeaders` + `u()`, owns an 8s `AbortController`, and
**never** calls `clearAccessKey`. **(R3-M3)** The helper does NOT itself decide
`needs-auth` — on the soft-auth route a bad key returns the 200 skeleton, so
`needs-auth` is a store-level decision (skeleton-vs-stored-key, §4.2), not an HTTP
status. The helper returns `ok:true` with whatever JSON came back (skeleton or
full); the only failure reasons are `unreachable` (network/5xx/abort). The 401
branch is kept purely as a belt for a mis-deployment under a hard-auth dependency.

```ts
// api.ts — NEW export. Does NOT throw (unlike api()); never clears the key.
export type StatusResult =
  | { ok: true; data: any }                         // skeleton OR full doc; store decides needs-auth
  | { ok: false; reason: "unreachable" };

export async function getStatus(timeoutMs = 8000): Promise<StatusResult> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);   // dead VM flips dot ≤8s
  try {
    const res = await fetch(u("/api/system/status"), {
      headers: authHeaders(), signal: ctrl.signal,
    });
    if (!res.ok) return { ok: false, reason: "unreachable" };   // 5xx/4xx → unreachable
    return { ok: true, data: await res.json() };                // 200 skeleton or full
  } catch {
    return { ok: false, reason: "unreachable" };          // network OR 8s abort
  } finally { clearTimeout(t); }
}
```

### 4.2 Health store — `web/src/health.ts` (new, `useSyncExternalStore` singleton)

A module-level singleton (NOT threaded through `AuthCtx` — FE MUST-FIX 4) so
consumers subscribe to slices and a 20s poll never re-renders the whole authed
tree. Mirrors the existing `TTS_ON_EVENT` bus pattern.

```ts
export type CapState =
  "unknown"|"warming"|"ready"|"failed"|"unavailable"|"absent"|"degraded";
export type ServerHealth = "ok"|"unreachable"|"needs-auth"|"unknown";
export type Reachability = "online"|"server-unreachable"|"browser-offline";

export interface Capabilities {
  // llm.state is the AUTHORITATIVE has_credentials() rollup ("ready"|"absent"|"degraded").
  // providers is INFORMATIONAL ONLY (ModelPicker per-provider hint) — never gates.
  llm: { state: CapState; verified: boolean|null; providers: { anthropic: boolean; xai: boolean } };
  embeddings: { state: CapState; last_error: string|null; since: number };
  transcription: { state: CapState; last_error: string|null; since: number; model?: string; compute_type?: string };
  push: { state: CapState };
  geocoder: { state: CapState };
  db: { state: CapState; last_error?: string|null };
}

interface HealthModel {
  reachability: Reachability;
  server: ServerHealth;
  caps?: Capabilities;
  lastOkAt: number | null;
  observed: { last5xxAt?: number; lastNetErrAt?: number; lastStallAt?: number;
              llmFailAt?: number; llmOkAt?: number };
}
```

#### The ONE envelope adapter (R3-M1)

Both `/api/auth/verify` and `/api/system/status` carry the **same** `capabilities`
sub-object (§3.4). A single function ingests either envelope:

```ts
// The ONLY place a server envelope becomes Capabilities. Both /verify and /status
// nest the identical `capabilities` object, so there is no per-envelope branching.
export function ingestVerify(data: any): void {            // also used for /status
  if (data?.capabilities) setCaps(data.capabilities as Capabilities);
}
```

- The boot effect (`App.tsx:102`) calls `health.ingestVerify(v)`.
- `connect()` (`App.tsx:81`) calls `health.ingestVerify(v)` **and**
  `health.refreshNow()` (fire the first authed poll immediately).
- The poller calls `getStatus()` → on `{ok:true,data}` calls the SAME `ingestVerify(data)`.

This kills the dual-envelope drift risk: one shape, one adapter, three call sites,
both seeding paths covered.

#### Reconciliation (declared wins; observed can only *downgrade*)

**Rule 0 — `needs-auth` (R3-M3, skeleton-vs-stored-key).** On a poll result
`{ok:true,data}`: if `data.capabilities === undefined` (a 200 **skeleton**) **AND**
`getAccessKey()` is non-null → set `server:"needs-auth"` (the stored key no longer
verifies; the soft-auth route silently downgraded us to skeleton). This is the
ONLY `needs-auth` trigger; there is no reliance on `status===401`. It never logs
out — it only tints the dot amber with "Re-authenticate." If `data.capabilities`
is present → `server:"ok"` and ingest caps.

1. `navigator.onLine === false` → `reachability:"browser-offline"` (dominates;
   don't fetch).
2. else recent `neterr`/`stall` **AND** no server byte within ~8s →
   `reachability:"server-unreachable"` (FE MUST-FIX 6 conjunction — a single
   transient 5xx must NOT flip red).
3. else `reachability:"online"`; `server` from the last poll
   (`ok`/`needs-auth` per rule 0 / `unreachable`).
4. `caps.llm`: a recent `llmFailAt` (≤60s) with no newer `llmOkAt` → render
   `degraded`; the next `llmOkAt` or poll re-asserts `ready`. Self-healing, zero
   tokens.

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

**`streamChat` (`api.ts:735-741`) — the initial fetch is OUTSIDE the read-loop
`try` (which starts at `:755`); ADD a try/catch around it, AND add the `stalled`
flag (R3-M2):**

```ts
// R3-M2: distinguish a real 90s stall from a user-initiated abort (leaving chat /
// new turn, via the `signal` wired at :731-733). BOTH hit the catch{break} at :759.
let stalled = false;
const STALL_MS = 90000;
const arm = () => { if (idle) clearTimeout(idle);
  idle = window.setTimeout(() => { stalled = true; ctrl.abort(); }, STALL_MS); };

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

And at the read-loop catch (`:758-759`), report `stall` ONLY when the watchdog
fired — a plain user abort reports nothing:

```ts
try { r = await reader.read(); }
catch {
  if (stalled) { health.report({ kind: "stall" }); /* + trigger out-of-band poll */ }
  break;                                   // user-abort (stalled===false) → silent, no report
}
```

A `{type:"error"}` chat event → `report({ kind: "llm-fail" })`; a clean `done` for
an LLM turn → `report({ kind: "llm-ok" })` (fast self-heal).

**`streamSSE` (`api.ts:803-838`) — same shape (R3-M2 + R3-M5):** the initial fetch
is outside the read-loop try (`:818`), and the watchdog `arm()` (`:816`) +
`abort():837` have the identical ambiguity. Add the same `stalled` flag to its
`arm()`, the same try/catch around the `:806` fetch, and the same gated `stall`
report in its `:821` catch. **(R3-M5)** Because `streamSSE` (rebuild/research) also
yields `{type:"error", message}` (`:797`) and a `{type:"done"}` (`:795`), wire its
error event → `report({kind:"llm-fail"})` and its clean `done` → `report({kind:"llm-ok"})`
too — so a revoked key surfaces as `degraded` from a rebuild run, not only chat.
This makes the "downgrade LLM health from real request outcomes" claim true for
both LLM stream paths.

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
- On each tick: drain observed signals, call `getStatus()`, apply rule 0 + merge
  (observed can only downgrade), `ingestVerify(data)`, update the store.
- `refreshNow()` is the single immediate-poll entrypoint; `connect()` calls it
  (§4.2) so a fresh login fires the first authed poll at once.
- **Never** calls `clearAccessKey`. `needs-auth` only flips the dot.

### 4.5 Mount + carve-out — `web/src/App.tsx`

Mount the poller **above the auth gate** (so KeyEntry gets pre-auth reachability),
but **carve out `/share/:token`** (FE MUST-FIX 3) so the poller never runs on a
public share recipient's device. Given the verified structure (share route at
`:124`, `path="*"` element at `:125`, KeyEntry `:127`, Shell `:129`, OwnerChatPage
`:138`):

- Wrap only the `path="*"` element (auth + KeyEntry + Shell tree) with the
  `StatusProvider`/poller — NOT the `/share/:token` route.
- The poller therefore runs on KeyEntry and Shell but never on SharePage.
- The status route is soft-auth, so even a stray pre-auth call returns the
  harmless `{ok,brain,ts}` skeleton (defense in depth).
- **Seed the store from BOTH boot and login (R3-M1):** the boot effect (`:102`)
  and `connect()` (`:81`) each call `health.ingestVerify(v)`; `connect()` also
  calls `health.refreshNow()`.
- **Do NOT touch `App.tsx:106`.** The poller is independent of the boot `/verify`;
  both may fire near-simultaneously (different routes, neither mutates the other).

**(R3-L4) KeyEntry reachability slot.** `web/src/components/KeyEntry.tsx` renders
a one-line reachability banner ("Can't reach {brain}" vs "You're offline") above
its form, reading `useHealth(m => m.reachability)`. The provider mounts above the
gate, so KeyEntry can read the store pre-auth; the poll uses the skeleton path (no
key required) to learn reachability before login. The full detail panel stays in
Shell.

### 4.6 Indicator UX — `web/src/components/Shell.tsx`

Insert `<StatusDot/>` near the brand (`Shell.tsx:240`, beside `<ReviewBell/>` at
`:243`); tap expands a detail panel (reuse `Modal.tsx` / the banner slot).

Dot color (worst-of, via `useHealth` slices):
- **green:** `reachability==="online"`, `server==="ok"`, all subsystems
  `ready` (incl. `llm: ready`).
- **amber:** any subsystem `warming`; OR `llm` `absent`/`degraded`; OR
  `transcription` `unavailable`; OR `push`/`geocoder` `absent`; OR
  `server==="needs-auth"` (label "Re-authenticate").
- **red:** `reachability==="server-unreachable"` OR any subsystem `failed`
  (incl. `db: failed`).
- **grey:** `unknown` (pre-first-poll) / `browser-offline`.

**(R3-H2)** The `llm` axis reads `caps.llm.state` (the `has_credentials()` rollup),
so the dot can no longer be green while the share landing says "unavailable" — they
share one predicate. The `providers` map is used ONLY by the ModelPicker per-provider
hint, never by the dot.

Copy distinguishes `unknown` ("checking…") from `warming` ("starting up — try
again shortly"). Panel lists Server (reachable / unreachable / re-authenticate,
"last ok Ns ago"), AI (ready / not configured / last request failed — degraded),
Semantic search, Transcription (ready / warming / not installed / failed+error),
Push, Geocoder, DB.

**Replace** the `navigator.onLine`-only offline banner (`Shell.tsx:261`) with the
three-axis signal: `browser-offline` → "Offline — reading cached notes only";
`server-unreachable` (browser online) → "Browser online, but JBrain server
unreachable" (research §5/§7.3 gap). Keep the version-mismatch banner
(`:258-259`); keep `useOnline` (`hooks.ts:264`) feeding the `browser-offline` axis.

### 4.7 Gating — primitives + Plan C inventory wholesale

#### Single-sourced copy — `web/src/capabilities.ts` (new)

```ts
export const CAP_COPY: Record<string, Partial<Record<CapState, string>>> = {
  llm: {
    absent:   "AI features need an API key for the active provider — set the key for your LLM_PROVIDER on the server.",
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

(LLM copy reworded for the active-provider predicate — R3-H2: it is no longer about
"which named key" but "the active provider has no usable key.")

#### Shared primitives — `web/src/components/Capability.tsx` (new)

- `useCapability(id)` reads the store → `{ ready, state, reason, providers? }`.
  For `llm`, `ready` derives from `caps.llm.state === "ready"` (the
  `has_credentials()` rollup) — `providers` is exposed only for the ModelPicker
  hint. Single poller folds in `ModelPicker.tsx`'s private `/verify` re-fetch
  (`:37,48-52,61`).
- `<RequiresCapability id mode="hide"|"disable"|"note">`,
  `<CapabilityButton cap>` (sets `disabled`, `title={reason}`, `aria-disabled`;
  `warming` → spinner + "available shortly" tone; `failed`/`unavailable`/`absent`
  → danger tone), `<CapabilityNote id>`.
- Keep `auth.hasLlm` derived from the store's `llm.state` so the one existing
  consumer (`Attachments.tsx:38`) needs no change until its row is migrated.

#### Re-verified feature → capability inventory (Plan C, wholesale)

Principle: degrade where a non-AI fallback exists; block-and-explain otherwise.
Every `llm` gate below uses the `has_credentials()` rollup (`caps.llm.state`).

| Feature | File / anchor | Required cap | Degrade / copy |
|---|---|---|---|
| Chat **Entry** modes (Generic/Medical/Financial) | `Chat.tsx` MODES `:55-59`, seg `:858-876`, `online` `:117,505,922` | online only | Already `!online`-gated; **no LLM** — keep usable with no key (local capture) |
| Chat **Research** mode | seg `:858-866`, used `:519,669` | `llm` + online | Disable seg cell; if selected while `llm` not ready, replace safety line `:928` with `CapabilityNote("llm")` + disable Send |
| Chat **Full Brain** mode | seg `:858-866`, `:669` (`"assisted"`) | `llm` + online | Same as Research |
| Research **Deep** toggle | `Chat.tsx:939-941` | inside Research | covered transitively |
| Chat **Attach file** | `Chat.tsx:943-945` | online (local) | no LLM gate |
| Chat **Send** | `Chat.tsx:946-947` | online + (llm if chat mode) | add `(mode!=="entry" && !llmReady)` to existing `disabled` |
| Lab extraction on Medical PDF | `Chat.tsx:558-562` | `llm` | already best-effort try/catch; add `CapabilityNote("llm")` near Medical sub `:879-890` |
| Research approve/skip proposal | `Chat.tsx:713-727`, buttons `:826-827` | inside Research | transitive |
| Search **keyword** | `SearchPage.tsx:36,93-97` | none (FTS) | always available — safe default |
| **Search semantic** mode | `SearchPage.tsx:36,43-44,71-82,93-97` | `embeddings ready` | **R3-M4 (HARD):** on mount, if `mode==="semantic"` && embeddings not `ready` → `setMode("hybrid")` BEFORE the first query (URL can seed `mode=semantic` at `:43-44`); disable the `semantic` button while not ready; re-enable when a poll reports `ready`. Never fires a pure-semantic request during warmup → no `[]` "No results." |
| Search **hybrid** mode | `SearchPage.tsx:36,44,93-97` | embeddings preferred | selectable while `warming`; runs keyword-only safely (§3.5 fix); "(keyword only — semantic loading)" note |
| Search **entities** mode | `SearchPage.tsx:36,93-97` | always | no gate |
| Image **Analyze with AI** | `Attachments.tsx:290` (`hasLlm && isImage`) | `llm` | migrate `hasLlm` → `useCapability("llm")`; reflects `degraded` |
| **Transcribe** (audio/video) | `Attachments.tsx:284` (ungated today) | `transcription ready` | `warming` → disabled "loading model…"; `unavailable`/`failed` → `CapabilityNote` |
| **Video** transcribe → vision summary | server-side `audio_transcription.py:251-252` (`has_credentials()`) | `llm` | on video + `llm` not ready, `CapabilityNote` "Transcript only — the visual summary needs an AI key." |
| Attachments help copy | `Attachments.tsx:197` (`hasLlm ? …`) | `llm` | switch conditional `hasLlm` → `useCapability("llm").ready` |
| Note **AI Analysis** ↻ | `AiAnalysisPanel.tsx:63`, mounted `NotePage.tsx:219` | `llm` | `CapabilityButton`; read-only sidecar + note when not ready |
| Note **Rebuild/Draft/Regather/Guide/Redraft** | `RebuildPanel.tsx:7`, buttons `:259-292`, mounted `NotePage.tsx:372` | `llm` (+embeddings for gather quality) | gate entry control; embeddings `!ready` → "keyword-only sources" note (gather degrades safely via §3.5) |
| Note **TalkPanel** (KB notes) | `NotePage.tsx:221` (KB only) | `llm` | `CapabilityButton` on send |
| **Labs** Extract/Re-analyze | `LabImportPanel.tsx:58` | `llm` | gate THAT button |
| **Entities** identity edits | `entities.py:40-66`, polled `EntitiesPage.tsx:66-77,96` | `llm`+`embeddings` | pre-edit `CapabilityNote`; surface `last_error` from the status poll |
| **Map** address labels | `MapPage.tsx:212` | — | **DROPPED** (FE 5): labels pre-resolved; client never geocodes; `geocoder` is diagnostic-only in the panel |
| **Owner-assisted chat** route | `App.tsx:138` (`OwnerChatPage`, `/shares/chat/:linkId`) | — | **DROPPED** (FE 5): E2EE human↔human, opaque ciphertext, NOT LLM. No gate |
| **Push subscribe** | `Shell.tsx:15-93` (ReviewBell) | `push` | one-line note when server `push` absent |
| **Public Guided/Research share** | `SharePage.tsx:66-74` → `GuidedChat`/`ResearchChat` | `llm` (server) | server-driven (§3.6): landing `llm_ready===false` → "temporarily unavailable" instead of the chat. **Same `has_credentials()` predicate as the dot (R3-H2).** |
| **Encrypted share chat** (`kind="chat"`) | `SharePage.tsx:77-78` → `ChatShareGuest` | none | E2EE, no gate |
| **ModelPicker** missing-key warning | `ModelPicker.tsx:37,48-52,61` | `llm.providers` (INFORMATIONAL) | replace private `/verify` re-fetch with `useCapability("llm").providers`; this is the ONE place the providers map is consumed |
| Any AI/embeddings action while **unreachable** | global | `reachability` | `CapabilityButton` disabled, tooltip "Server unreachable — retrying…" |
| `/flows`, `/actions` editors | `WorkflowsPage`, `ActionsPage` | run-time only | config editors; soft per-trigger note (LOW) |

This table is the maintained artifact; a checklist comment at the top of
`capabilities.ts` and `AdvancedHome.tsx` points here.

### 4.8 Toast — `web/src/components/Toaster.tsx` (new, ~80 lines, no dep)

1. `useToast()` + a `Toaster` mounted once near `Shell`. Non-blocking,
   dismissible, stacked, auto-expire. Fed by the same `health.ts` bus.
2. **Replace blocking `alert()`** in Chat/NotePage/Attachments with toasts
   carrying `ApiError.message`; composer rollback stays.
3. `ApiError.category` ("auth"|"network"|"unavailable"|"validation"|"server")
   inferred from status in the existing `api.ts:46-53` parse block — shared by the
   toast and the dot so they agree on classification.
4. `explainError(err, capHint?)` consults the live store + `CAP_COPY`: a
   503/feature failure that slipped a gate shows the *same* copy the gate would.
5. Promote key silent `.catch(()=>{})` loaders to a quiet "Couldn't load X" toast
   **only when `server==="ok"`**.
6. **Keep SearchPage's `:79` catch swallowed** (FE MUST-FIX 7) — do NOT route it
   through the toast (per-keystroke storm on cold boot).
7. SSE stall watchdog: on a **real** stall (the `stalled` flag, R3-M2) it also
   `report({kind:"stall"})`; a user abort emits nothing (no spurious toast/poll).

---

## 5. Constraint-compliance checklist (research §8)

- **Offline-tolerant auth** ✓ `getStatus`/poller never call `clearAccessKey`;
  `needs-auth` (rule 0) only flips the dot; observed feed leaves `api()`'s
  401-throw intact; sole logout stays `App.tsx:106`.
- **Cross-origin** ✓ bearer via private `authHeaders()` + `u()`; CORS `*`,
  `allow_credentials` off (`main.py:232-242`); no cookies on the status path.
- **No token burn** ✓ LLM `ready`/`absent` from `has_credentials()` +
  `verified:null`; `degraded` derives from observed real traffic, never a
  synthetic probe; zero model calls per poll.
- **Cheap & frequent** ✓ status = in-memory reads + one `SELECT 1`; 5s warming /
  20s steady; paused when hidden; 8s abort; single-flight.
- **Graceful degradation** ✓ search → keyword (server fix §3.5 + on-mount
  force-hybrid §4.7); capture still saves + re-indexes; `warming` vs
  `unavailable`/`failed` honest copy.
- **No new heavy deps** ✓ React + `useSyncExternalStore` + `setInterval` + a
  ~40-line bus + ~80-line toast.
- **Security** ✓ public skeleton exactly `{ok,brain,ts}` (two builders,
  allowlist-tested, `now_utc().isoformat()` — no throw); detail authed-only;
  `last_error` `[:200]`, post-auth only.
- **Single-worker** ✓ LOUD comments in both readiness modules.
- **One predicate** ✓ `has_credentials()` drives dot, gates, and share landing;
  providers map is informational-only (one consumer: ModelPicker).

---

## 6. Ordered, dependency-aware implementation phases

1. **Backend readiness + bug fix.** `embeddings.readiness()` + `audio_transcription.readiness()`
   (locked) with LOUD single-worker comments; **search.py §3.5 fix**. Tests.
2. **Backend assembler + endpoints.** `system_status.capabilities()` (**llm from
   `has_credentials()`**, R3-H2); new soft-auth `routers/system_status.py` (two
   builders, **`now_utc().isoformat()`**, R3-H1) registered at `main.py:244`;
   extend `/api/auth/verify` with the same `capabilities` shape. `share.py`
   `llm_ready` on both landings. curl-verifiable alone.
3. **Client core.** `getStatus()` in `api.ts`; `health.ts` store + **the one
   `ingestVerify` adapter** + reconciliation **rule 0** (R3-M3) + `useHealth`;
   `useHealthPoll` (adaptive cadence, backoff, 8s abort, resume handlers,
   `refreshNow`).
4. **Observed feed.** Add try/catch to `api()`, `streamChat` (`:735`), `streamSSE`
   (`:806`); add the **`stalled` flag** to both watchdogs (R3-M2); wire `report()`;
   stall → re-poll; client-only LLM downgrade + `llm-ok` self-heal on BOTH chat and
   rebuild SSE (R3-M5); `ApiError.category`.
5. **Mount + indicator.** `StatusProvider`/poller above the gate **with the
   `/share/:token` carve-out**; seed from boot AND `connect()` (R3-M1); `StatusDot`
   + detail panel in `Shell.tsx`; three-axis banner; KeyEntry reachability line
   (R3-L4).
6. **Toast.** `Toaster` + `useToast`; replace `alert()`s; `explainError`; promote
   silent catches (server-ok only); keep SearchPage `:79` excluded.
7. **Gating sweep.** `capabilities.ts` (`CAP_COPY`) + `Capability.tsx` primitives;
   walk the §4.7 inventory (Attachments + ModelPicker first to prove the primitives
   + fold the duplicate `/verify` poll), then Chat modes/Send/lab-note,
   **SearchPage on-mount force-hybrid (HARD, R3-M4)**, NotePage AI/Rebuild/Talk,
   Labs Extract, Entities note, SharePage server-driven landing, push note.

Phases 1→2 = backend, curl-verifiable. 3→5 unlock the indicator. 6→7 per-feature.

---

## 7. Testing strategy

**Backend (pytest):**
- embeddings: `unknown→warming→ready`; `ImportError`→`unavailable`; other
  exception→`failed` + `last_error`; `_model is None` after `readiness()` (no load).
- audio **reload regression:** load A → `ready`; monkeypatch `audio_model()` → B;
  `readiness().state == "warming"` BEFORE reload, then `ready` after `_get_model()`.
- audio `ImportError` → `unavailable`, state set before `TranscriptionUnavailable`.
- **lock:** `_set_state`/`readiness()` share `_state_lock`; readiness does NOT take
  `_model_lock` (poll returns while a fake slow load holds `_model_lock`).
- `capabilities()`: includes llm/embeddings/transcription/push/geocoder/db; `db` →
  `failed` on raise; `verified is None`; `llm.complete` NOT called.
- **R3-H2:** `capabilities().llm.state == "ready"` iff `llm.has_credentials()` is
  monkeypatched true; with `LLM_PROVIDER=xai` + only `LLM_API_KEY` set →
  `has_credentials()` false → `llm.state == "absent"` while
  `providers.anthropic == true` (proves the dot follows creds, not presence). Assert
  `share.llm_ready()` returns the SAME boolean as `capabilities().llm.state=="ready"`.
- **R3-H1:** `/api/system/status` does NOT raise `AttributeError`; `ts` parses as
  ISO-8601 with a `+00:00` offset; assert `hasattr(clock,"iso_now") is False`
  (guards against an implementer re-introducing the bad call).
- `/api/system/status`: **public skeleton exact allowlist** — unauthed body keys
  `== {"ok","brain","ts"}`, `version`/`capabilities` ABSENT; authed body has
  `version` + `capabilities`; one `SELECT 1`, no write.
- `last_error` never appears in `/api/health` or `/auth/info`.
- **R3-M4 server side:** `search(mode="semantic")` returns `[]` (not a 500) when
  `_get_model` raises (proves the empty-result risk the client force addresses);
  `search(mode="hybrid")` returns keyword hits under the same monkeypatch.
- guided/research landings include `llm_ready`; `start`/`turn` 404 when false.

**Frontend (vitest + RTL):**
- `getStatus`: 5xx→`unreachable`; network→`unreachable`; 8s abort→`unreachable`
  (fake timers); 200 skeleton→`{ok:true}`; **never** calls `clearAccessKey` (spy).
- **R3-M3:** poll returns a 200 skeleton (`capabilities===undefined`) while
  `getAccessKey()` non-null → store `server==="needs-auth"` (dot amber
  "Re-authenticate"), NO logout; full doc → `server==="ok"`. Skeleton with NO
  stored key → `server` stays `ok`/`unknown`, not `needs-auth`.
- **R3-M1:** `ingestVerify` ingests `/verify` AND `/status` envelopes into the same
  `caps` (one adapter); `connect()` seeds the store (caps present immediately, no
  `unknown` flash) AND calls `refreshNow`; boot effect also seeds.
- **R3-M2:** simulate a watchdog fire (`stalled=true`) → exactly one `stall` report;
  simulate a user abort (`ctrl.abort()` with `stalled=false`) → ZERO `stall`/`neterr`
  reports and no out-of-band poll. Same for `streamSSE`.
- **R3-M5:** a rebuild `{type:"error"}` → `llm-fail`; a clean rebuild `done` →
  `llm-ok`.
- store: selectors don't re-render unrelated consumers; `llm-fail`→`degraded`, then
  `llm-ok`/poll→`ready`; observed never upgrades; **5xx alone NOT red** (needs
  neterr/stall AND no-byte-8s); 401 still throws from `api()`.
- carve-out: poller does NOT mount on `/share/:token`.
- **R3-M4 client:** mount SearchPage with URL `?mode=semantic` while embeddings not
  `ready` → mode flips to `hybrid` BEFORE any `/api/search` call; the `semantic`
  button is disabled; NO request with `mode=semantic` is ever fired during warmup;
  once a poll reports embeddings `ready`, the button re-enables.
- `useCapability`/`CapabilityButton`: each state → enabled/disabled + correct
  `CAP_COPY`/`title`/`aria-disabled`.
- dot color table (incl. `llm:absent`→amber, `db:failed`→red); `unknown` vs
  `warming` copy; three-axis banner.
- **copy-exhaustiveness test:** every `(capId, reachable state)` has `CAP_COPY`;
  every `CAP_COPY`/inventory cap exists in `Capabilities`.
- toast: `alert()` replaced; SearchPage `:79` NOT toasted; silent-load toast only
  when server ok.

**Manual:** no faster-whisper → transcription `unavailable`, button disabled,
capture/keyword search work; edit audio model in Settings → dot amber (warming)
immediately; kill server → red dot ≤8s, no logout, cached pages render; rotate key
→ amber "Re-authenticate" (skeleton-vs-stored-key, R3-M3), not red, no logout;
revoke LLM key mid-session, send a chat OR run a rebuild → amber `degraded` + toast,
green on next valid call (R3-M5); **set `LLM_PROVIDER=xai` with only the Claude key
→ dot shows AI `absent` AND share landing says "unavailable" (they agree, R3-H2)**;
**open `/search?mode=semantic` during cold boot → it runs hybrid (keyword) results,
not "No results" (R3-M4)**; leave a chat mid-stream → NO stall toast/red flicker
(R3-M2); cross-origin (Pages → VM); KeyEntry distinguishes "server down" vs "you're
offline"; cold boot `warming→ready` within one 5s tick; share link with no usable
key → "temporarily unavailable."

---

## 8. Residual risks & out-of-scope (honest)

**Accepted residual risks:**
1. **"Real-time" is poll + observed-outcomes, not push.** Silent subsystem death
   with no traffic lags up to 20s; any failure on real traffic surfaces instantly.
2. **LLM validity is observed, not proactive.** A revoked/over-quota key shows
   green until the first real call (chat OR rebuild, R3-M5) fails → then `degraded`
   + toast. Cost-driven. Affects the public-share landing flag too (the existing
   `start`/`turn` 404 is the backstop). Note: `has_credentials()` proves the active
   provider has a key, not that the key is valid.
3. **Per-process, in-memory readiness.** Single worker today
   (`server/Dockerfile:45`); LOUD comments in both modules.
4. **Observed false positives.** A single transient 5xx briefly tints amber;
   bounded by short decay, declared-state precedence, and the
   "neterr/stall AND no-byte-8s" conjunction before red. The R3-M2 `stalled` flag
   removes the user-abort false-stall class entirely.
5. **Gating drift.** The §4.7 inventory is hand-maintained; mitigated by
   single-sourced `CAP_COPY`, the exhaustiveness test, ~1-line primitives, checklist
   comments, and the toast backstop.
6. **`needs-auth` skeleton heuristic (R3-M3).** Detection is "200 skeleton + stored
   key." A genuinely deauthed device that ALSO loses network reads as `unreachable`
   first (reachability dominates); the rotated-key→amber UX appears once the server
   is reachable again. Acceptable for a single-user app; never causes a logout.
7. **Readiness tuple micro-tear on audio `_model_key`.** `readiness()` reads
   `_model_key` without `_model_lock` (by design); worst case one extra `warming`
   tick.

**Explicitly out of scope:**
- **SSE / WebSocket push.** Transport-agnostic store/indicator/gating/observed feed
  make SSE a clean additive future layer if the deployment goes multi-user or adds
  an ops dashboard. Not built now.
- **Proactive LLM key validation** (would burn tokens).
- **Multi-worker readiness sharing** (no `--workers` today).
- **Scheduler heartbeat** (cut; low value).
- **Pydantic `CapState`/`Capability` models** (§3.9 — deferred; no OpenAPI consumer).
- **`/flows`/`/actions` deep run-time gating** (config editors; soft note only).
