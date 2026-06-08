# Plan D — Truly Real-Time, Stream-Driven + Observed Health

**Author:** Architect D · **Repo:** /home/user/JBrain

## Thesis
Health is two things the current app conflates into nothing: (1) **server-pushed readiness transitions** (embeddings warming→ready, audio failed, LLM key present/absent) delivered the instant they happen over an SSE status stream that reuses the exact transport `streamChat`/`openChatStream` already use; and (2) **passively observed outcomes of real traffic** — every `api()` and `streamChat` call already sees 5xx/network/LLM errors, so those feed a client-side health store. The indicator reflects what is *actually* happening to real requests, reconciled against authoritative server state, with a heartbeat fallback so it degrades to polling when SSE can't get through.

## Verified ground truth (corrections)
- `GET /api/health` (`main.py:254-256`) `{ok,brain}` liveness. ✔
- `/api/auth/verify` (`auth_router.py:22-38`) manifest. ✔
- Embeddings/audio warmups (`main.py:177-202`, `208-215`) are fire-and-forget `asyncio.create_task`; **neither sets readiness** — nowhere to read state today. ✔
- Lazy getters `embeddings._get_model` (`:20-30`), `audio_transcription._get_model` (`:93-110`, raises `TranscriptionUnavailable`). ✔
- **SSE proxy buffering is the sharpest constraint.** `Caddyfile.template:33-37` only disables buffering for `path /api/chat/*` (`flush_interval -1`); chat router sets `X-Accel-Buffering: no` + `: keepalive\n\n` every 15s (`chat.py:163-165,183`). **Any new SSE endpoint NOT under `/api/chat/*` will be buffered by Caddy.** Decisive for endpoint path.
- CORS: `allow_credentials` OFF, origins from `JBRAIN_CORS_ORIGINS` default `*` (`main.py:232-242`). Bearer-only authed; cookies don't ride → SSE must be bearer-authed via **fetch+ReadableStream reader**, not native `EventSource` (can't set `Authorization`).
- Offline-tolerant auth: `App.tsx:97-107` keeps user authed on non-401.
- Frontend errors: blocking `alert()` (`Chat.tsx:354,363,480,514-515,934`), silent `.catch(()=>{})`, no toast. `useOnline` `navigator.onLine`-only (`hooks.ts:264-277`).
- Existing gating: only `Attachments.tsx:38,290` via `hasLlm`. `AuthState` `App.tsx:33-45`.

---

## (a) Backend: readiness registry + event publication + SSE endpoint

### a.1 Registry (`server/app/services/health.py`, new)
Tiny thread-safe in-process registry storing cached state set by warmers; never probes on read.
```python
State = Literal["unknown","warming","ready","degraded","unavailable"]
_subs: dict[str,_Subsystem] = {n:_Subsystem(n) for n in ("embeddings","audio","llm","db","push","geocoder")}
_generation = 0; _subscribers: set[asyncio.Queue] = set(); _loop = None
def bind_loop(loop): ...
def set_state(name,state,detail=None):   # thread-safe; callable from worker threads
    # idempotent; bumps _generation; _publish(evt) to live SSE subscribers via call_soon_threadsafe
def snapshot() -> dict: # {gen, subsystems:{name:{state,detail,changed_at}}}
async def subscribe() -> asyncio.Queue: ...
def unsubscribe(q): ...
```
State machines: embeddings `unknown→warming→ready|unavailable`; audio same (`unavailable` is *expected* on minimal installs → UX "feature off", not error); llm derived at boot from `settings.has_llm` (`ready`/`unavailable`), **never probed**, only downgraded to `degraded` by observed runtime errors; db `ready` after `init_db`; push/geocoder from config.

### a.2 Wire warmers (`main.py`)
At lifespan start `health.bind_loop(asyncio.get_running_loop())`; seed config states (llm/push/geocoder/db). In `_warm_embeddings` (`:177`): `set_state("embeddings","warming")` first, `ready` after `_get_model`, `unavailable` in except (`:200`). Same for `_warm_audio` (`:208`).

### a.3 SSE endpoint — **under `/api/chat/` to inherit Caddy's unbuffered proxy**
Decision: `GET /api/chat/health/stream` (+ snapshot `GET /api/chat/health`). Inherits `flush_interval -1` (`Caddyfile.template:34`) with **zero Caddy change**, avoiding the buffering trap for self-hosted deploys. (Alternative: new `/api/health/*` + Caddy `@sse` rule — rejected, requires every operator to re-run install/edit Caddy. Documented as optional cleanup, not depended on.)

Add to `chat.py` (already has `StreamingResponse`, `CurrentUser` on router, keepalive pattern):
```python
@router.get("/health")          # snapshot
def health_snapshot(): return health.snapshot()

@router.get("/health/stream")   # stream
def health_stream(since: int = 0):
    async def gen():
        yield f"event: snapshot\ndata: {json.dumps(health.snapshot())}\n\n"  # replay immediately
        q = await health.subscribe()
        try:
            while True:
                try: evt = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError: yield ": keepalive\n\n"; continue
                yield f"event: subsystem\ndata: {json.dumps(evt)}\n\n"
        finally: health.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
```
Events: `snapshot {gen,subsystems{...}}`; `subsystem {type,name,state,detail,gen,at}`; comment keepalive every 15s (= reachability heartbeat). Cost: one long-lived connection per tab, awaiting a queue.

### a.4 Observed LLM/runtime health (backend)
In `chat.py` `pump()` error path (`:153-155`) and architect, on a real provider error `health.set_state("llm","degraded","<error class>")`; a subsequent success `set_state("llm","ready")`. Reflects actual model failures (rate limit, bad key at call time) without a synthetic probe; auto-recovers.

---

## (b) Client: observed outcomes + reconciliation

### b.1 Health store + bus (`web/src/health.ts`, new)
Dependency-free pub/sub singleton (mirrors `TTS_ON_EVENT`), feeds React via `useHealth()` (`useSyncExternalStore`).
```ts
export interface HealthModel {
  link: "online"|"server-unreachable"|"browser-offline";
  server: { state: SubState; lastSeen: number };
  subsystems: Record<string,{state:SubState;detail?:string|null;source:"server"|"observed"}>;
  observed: { last5xxAt?: number; lastNetErrAt?: number };
}
```
Inputs: (1) pushed SSE events set `subsystems[name]={state,detail,source:"server"}` and stamp `server.lastSeen=now` (any byte incl. keepalive → `link:"online"`); (2) observed outcomes (b.2). API: `subscribe(cb)`/`getModel()`/`report(event)`.

### b.2 Feed `api()`/`streamChat` outcomes
Modify central wrapper `api()` (`api.ts:40-57`):
```ts
try {
  const res = await fetch(u(path), {...opts, headers: authHeaders(opts.headers)});
  health.report({kind:"http", status: res.status});
  if (res.status === 401) throw new ApiError("Not authenticated",401);
  ...
} catch (e) {
  if (!(e instanceof ApiError)) health.report({kind:"neterr"});
  throw e;   // unchanged behavior
}
```
`http status>=500` → `last5xxAt=now`, `server.state:"degraded"`; `<500` (incl 401/4xx) → server answering, `server.lastSeen=now`. **401 still throws unchanged** → `App.tsx:106` untouched. `neterr` → `lastNetErrAt=now`. Instrument `streamChat` (`:758-759`) and `streamSSE` (`:821`) catch blocks + initial fetch; stall-watchdog abort (`STALL_MS`) → `{kind:"stall"}` → mark server suspect + immediate snapshot re-fetch.

### b.3 Reconciliation (pushed vs observed vs browser)
1. `navigator.onLine===false` → `browser-offline` (dominates).
2. else recent neterr/stall AND no server byte within ~8s → `server-unreachable` (the key new signal: "my server is down" vs "my Wi-Fi is down").
3. else `online`, `server.state` = worst of {5xx in last 30s → degraded, else ready}.
4. Subsystem precedence: **server-pushed wins**; observed can only *downgrade* llm to `degraded` transiently (self-heals on next server event / successful turn); observed never *upgrades*.

### b.4 Stream lifecycle (`useHealthStream` started once in authed `App`)
Open `/api/chat/health/stream` via fetch+ReadableStream reader (bearer; like `openChatStream`). **Not** `EventSource`. Reconnect with capped backoff (1→2→5→15→30s); each reconnect re-fetches snapshot (server replays). Heartbeat watchdog: no byte ~25s (>15s keepalive) → reconnect; reconciliation flips to `server-unreachable` if reconnect fails. Pause when hidden (`visibilitychange`, grace period), reopen + snapshot on resume. Started only when authed (never `KeyEntry`/`SharePage`).

### b.5 Heartbeat fallback when SSE unusable
If stream fails to open (non-OK) or repeatedly dies <N s after open with no bytes (classic **proxy-buffered** symptom) → **poll mode**: `GET /api/chat/health` every 20s (+ focus/visibility). Same `snapshot` shape feeds same store → indicator + gating keep working, coarser latency. Subtle "live"/"polling" affordance in the panel only.

---

## (c) Real-time indicator UX
Status dot in `Shell.tsx:242` (beside `ReviewBell`), driven by `useHealth()`. Three-state link distinction is the headline:
- **browser-offline** (gray): `navigator.onLine===false`; reuses offline banner.
- **server-unreachable** (red): online browser but server not answering. New banner "Can't reach {brain} — your connection is fine but the server isn't responding." Genuinely new (today: nothing).
- **subsystem-degraded** (amber): server reachable, subsystem warming/degraded/unavailable; tap opens health panel (embeddings warming/ready/unavailable; audio ready/"not installed"; llm ready/"no key"/"errored — retrying").
- **all-green**: solid/hidden.
Transitions animate (amber→green) because they arrive as discrete pushed events — the real-time payoff. Version banner stays.

---

## (d) Pre-flight gating driven by live model
Capability map from merged model + `auth/verify`. Add `useCapability(name)` reading the store → `{enabled,reason,severity}`.

| Feature | Gate | Disabled copy |
|---|---|---|
| Semantic search | embeddings ready | "Search warming up — try again." / "unavailable on this server." |
| Chat AI, note analysis, rebuild, research shares | llm ready AND not server-unreachable | "AI needs an API key (none configured)." / "AI temporarily unavailable." |
| Transcribe (Attachments) | audio ready | "Transcription isn't installed on this server." (expected-off) |
| Image "Analyze with AI" | llm ready | existing + degrade on observed llm errors |
| Any write (capture/edits) | not offline/unreachable | "You're offline — changes can't be saved now." |

Mechanism: gated buttons `disabled` + tooltip (no blocking alert), reading the live store → enable/disable in real time as warming→ready arrives. Additive — never hides local-only capabilities.

---

## (e) Error surfacing tied to the bus
Lightweight toast in `Shell`, fed by `health.report({kind:"error",message,context})`. Replace blocking `alert()` (`Chat.tsx:354,363,480,514-515,934`) → non-blocking dismissible toasts (composer rollback stays). Silent `.catch(()=>{})` loads → `report({kind:"silent-load-failed"})` → quiet "Couldn't load X" if server healthy. `streamChat` `{type:"error"}` events also `report` → inline + nudge llm to `degraded`. Shared bus → a 5xx burst shows one coherent story (red dot + one toast), de-duped.

---

## (f) Section 8 compliance
Offline-tolerant (stream failures never touch key; 401 closes stream, doesn't clear key; `App.tsx:106` unchanged) · Cross-origin (fetch+reader with `Authorization`; `allow_credentials` off) · No token burn (llm config-derived + observed) · Cheap (one SSE/tab, in-memory snapshot, 15s keepalive, closes on background) · Graceful degradation (SSE→poll fallback, unavailable = "feature off") · No heavy deps (native fetch streaming + `useSyncExternalStore`) · Security (stream under authed `/api/chat`, never pre-auth).

---

## Ordered phases
1. Backend registry + warmer wiring (`health.py`, `main.py`).
2. SSE endpoint (`chat.py`); verify unbuffered through Caddy.
3. Client store + bus (`health.ts`); reconciliation; `useHealth`.
4. Observed feed: instrument `api()`/`streamChat`/`streamSSE`.
5. Stream client + poll fallback (`useHealthStream`).
6. Indicator + banners + panel (`Shell.tsx`).
7. Gating (`useCapability`, SearchPage/Chat/Attachments/composer).
8. Toasts replacing alerts/silent catches.
9. Backend observed-llm downgrade/recover.
10. Optional Caddy `@sse` generalization.

## Testing strategy
**Backend:** `set_state` transitions/idempotency/gen monotonicity/thread-safe from `to_thread`; snapshot shape; SSE integration (immediate snapshot, event on `set_state`, keepalive, 401 without bearer); scripted curl asserting first byte <1s + keepalives (catches buffering regression).
**Client:** reconciliation truth table (offline / neterr+no-byte / 5xx / pushed-ready beats observed-degraded / self-heal); store notifies; stream open→event→render; death→reconnect→snapshot; open-but-silent→poll fallback; visibility pause/resume; gating warming→ready flips button without reload; 401 still logs out, 5xx/neterr never do.

## Risks & tradeoffs (honest)
- **SSE connection cost**: long-lived socket per tab against browser ~6-per-host cap — the exact failure `streamChat` warns about (`api.ts:728-733`). Mitigations: close on background, single shared stream/tab, abort on logout. Chat+status = 2 of 6; real.
- **Caddy buffering** biggest deployment hazard. Sidestepped by living under `/api/chat/*`, but custom proxies (nginx default-buffers) still break → mandatory poll fallback. Status path named "chat" is slightly surprising; documented.
- **Complexity vs simple polling**: materially more code. Justified by the real-time mandate + observed-health (catching failures *between* polls). Snapshot + 15s poll alone delivers ~80% value; the stream is the differentiator, layered so it can be dropped.
- **Observed false positives**: single transient 5xx flashes amber. Mitigated by short decay + pushed-state precedence — trades stability for immediacy by design.
- **Reconnect storms**: capped backoff + always-replay-snapshot keeps correct if noisy.

### Critical files
- `server/app/services/health.py` (new), `server/app/main.py` (warmers `:177`/`:208`, bind loop, seed), `server/app/routers/chat.py` (`/health` + `/health/stream`; observed-llm `:153`), `web/src/health.ts` (new), `web/src/api.ts` (instrument `:40`,`:712`,`:803`), `web/src/components/Shell.tsx` + `web/src/App.tsx`.

---

## ~250-word summary
"Real-time" means two things the app does neither of — pushed server readiness transitions and observed outcomes of real traffic. The server pushes subsystem state changes over an SSE status stream reusing `streamChat`'s fetch+reader transport; simultaneously the central `api()`/`streamChat` wrappers — which already see every 5xx, network error, and stall — feed real failures into a client health store. Key moves: (1) a thread-safe backend `health.py` registry the warmers update, exposing snapshot + SSE; (2) the stream lives under `/api/chat/*` so it inherits Caddy's existing `flush_interval -1` with zero deploy changes — sidestepping proxy-buffering; (3) a client store merging pushed state (authoritative) with observed failures (transient, self-healing), uniquely distinguishing browser-offline vs server-unreachable vs subsystem-degraded; (4) live gating flipping buttons as warming→ready arrives; (5) a shared bus driving indicator + toasts replacing blocking alerts; (6) automatic 20s poll fallback when SSE is buffered/unsupported. No token burning, bearer-authed cross-origin, 401-logout untouched. Biggest weakness: the long-lived SSE socket competes against the browser's ~6-per-host cap that already bit `streamChat`, and adds real complexity vs simple polling — if unacceptable, the snapshot+poll path delivers ~80% of the value; the stream is the differentiator, not a dependency.
