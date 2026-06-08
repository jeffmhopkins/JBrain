# Research: Server / API Health Indication & Pre-flight Capability Gating

**Goal (from owner):** Real-time server *and* API health indication in the PWA,
and any service that won't actually work should tell you *before* you try to use it.

This document is the shared grounding for the planning workflow. All plan
authors and red-teamers should treat it as the source of truth for current
behavior (verify against the code; correct it if wrong).

---

## 1. Current health/status surfaces

| Surface | File | Auth | Returns |
|---|---|---|---|
| `GET /api/health` | `server/app/main.py:254` | public | `{ok, brain}` — pure liveness, says nothing about features |
| `GET /api/auth/info` | `auth_router.py:15` | public | `{brain_name}` (version withheld pre-auth) |
| `GET /api/auth/verify` | `auth_router.py:22` | key-gated | `{ok, brain_name, version, has_llm, app_tz, owner_set, llm_keys:{anthropic,xai}, vapid_public_key}` — the de-facto capability manifest |
| `GET /api/system/version` | `routers/system.py:113` | owner | `{current, latest, update_available, release_url, release_name}` (GitHub cached 1h) |
| `GET /api/system/stats` | `routers/system.py:137` | owner | storage, uptime, token usage, cost warnings |
| `GET /api/entities/status` | `routers/entities.py:30` | owner | `{rebuilding, status, generation, last_error}` |
| `GET /api/attachments/{id}/analysis-status` | `routers/attachments.py:115` | owner | `{status, detail, analyzed_at}` |
| update-log poll | `routers/system.py` + `UpdateConsole.tsx` | owner | live deploy log, with Caddy static fallback when API down |

There is **no consolidated capability/readiness endpoint**. Real status is split
across `verify`, lazy warmups with no flags, and per-feature endpoints.

## 2. How capabilities are determined

**Config-time (cheap, reliable):** `server/app/config.py`
- `has_anthropic = bool(llm_api_key)` (`LLM_API_KEY` or legacy `ANTHROPIC_API_KEY`)
- `has_xai = bool(xai_api_key) or provider in ("xai","grok")`
- `has_llm = has_anthropic or has_xai`
- VAPID push keys (env or DB-seeded), geocoder URL, auto-update sidecar (inferred from `COMPOSE_PROFILES=autoupdate`)
- `llm.has_credentials()` (`services/llm.py:506`) used for backend feature gating

**Runtime, lazy & opaque — the key gap:**
- **Embeddings (fastembed)** warmed async on boot via `_warm_embeddings()` (`main.py:177`). Lazy `_get_model()` (`services/embeddings.py:20`) under a lock. **No readiness flag exposed.**
- **Transcription (faster-whisper)** warmed async on boot via `_warm_audio()` (`main.py:208`), best-effort/non-fatal. `_get_model()` (`services/audio_transcription.py:93`) raises `TranscriptionUnavailable` if the package is missing. **No readiness flag exposed.**
- Consequence: the PWA cannot know whether semantic search / transcription is *warming*, *ready*, or *broken*. The first request just blocks or fails.

## 3. Startup / lifespan (`main.py:106-221`)

- **Synchronous, blocks boot (fail = release blocker):** `init_db`, `ensure_access_key`, workflow ingest, stale-run reset, image-analysis reset, entity-rebuild reset, `push.ensure_vapid`, action-def ingest, agent config validation, share safety assertions, pipeline validation.
- **Async, non-fatal (off event loop):** embeddings warmup + backfill re-index, audio warmup.
- **Background:** `_scheduler_loop()` every 60s (workflows, location triggers, trip detection, entity-rebuild watchdog, stale image-analysis cleanup); per-iteration errors swallowed.

## 4. Error handling

**Backend:** FastAPI `HTTPException` → `{detail: "..."}` everywhere. No custom
envelope, no error categories/codes. Status codes used sensibly (400/404/409/410/413/422/500).
Background tasks (push, image analysis, transcription) log-and-swallow.

**Frontend:** central wrapper `api<T>()` (`web/src/api.ts:40`):
- 401 → `throw ApiError("Not authenticated", 401)` (re-prompt)
- other non-OK → parse `detail` (incl. Pydantic arrays) → `ApiError(detail, status)`
- network errors thrown raw; caller must catch
- `streamChat()` SSE has a 90s stall watchdog (`api.ts:752`)
- On startup, a non-401 failure of `/api/auth/verify` keeps the user authed (offline-tolerant); only a real 401 clears the key (`App.tsx:97`).

## 5. Current status UI (minimal)

`Shell.tsx:258-261`:
- version-mismatch banner (`serverVersion !== PWA_VERSION`)
- offline banner driven by `navigator.onLine` (`useOnline` hook, `hooks.ts:264`) — **only knows browser network state, not whether *our server* is reachable/healthy**

`ErrorBoundary.tsx` catches render errors. No toast system. User-action errors
use blocking `alert()` + composer rollback (Chat/NotePage/Attachments). Many
loads silently swallow errors (`.catch(() => {})`).

## 6. Capability gating today (partial, ad-hoc)

Good example — Attachments (`web/src/components/Attachments.tsx`): `hasLlm` from
`AuthContext` hides "Analyze with AI" while leaving local "Transcribe" available
(local, no key). This is the *only* systematic example. Most LLM-dependent
surfaces (Chat modes, note analysis, rebuild, research, search) don't gate; they
let the call fail. `AuthContext` currently carries little beyond `hasLlm`.

## 7. The three gaps mapped to the goal

1. **No consolidated, real-time health/status** (liveness-only `/api/health`; readiness of embeddings/audio not even knowable client-side).
2. **Features that can't run aren't disabled** — gating exists only for attachments; embeddings/audio readiness isn't exposed at all.
3. **Errors aren't verbose/visible enough in the moment** — silent catches, blocking alerts, no "server up but a subsystem is down" signal, no real server-reachability check (vs. `navigator.onLine`).

## 8. Constraints / things any plan must respect

- **Offline-tolerant auth:** never log out on 5xx/network; only on real 401 (`App.tsx:97`). A health poller must not break this.
- **Cross-origin deploys:** PWA can run from GitHub Pages against a remote VM (CORS, bearer auth). Health polling must work cross-origin and tolerate the server being a different origin.
- **Cost:** LLM "health" must not burn tokens (don't make a real model call to prove the key works on every poll — distinguish "key present" from "key valid").
- **Cheap & frequent:** a real-time indicator implies polling or streaming; must be lightweight (the readiness endpoint must not do heavy work per call).
- **Graceful degradation is the existing philosophy** (fail-closed features, models load on demand). Don't regress that.
- **No new heavy deps** ideally; keep the PWA bundle lean.
- **Security:** don't leak capability details pre-auth beyond what `auth/info` already does.
