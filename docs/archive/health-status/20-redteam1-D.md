# Red-Team 1 — Critique of Plan D (Real-Time SSE Stream + Observed Health)

**Reviewer:** Red Team 1 · **Target:** `13-plan-D-realtime-stream.md` · **Date:** 2026-06-07

Verdict up front: Plan D is the most intellectually honest of the four about
what "real-time" means, and its observed-health idea is genuinely the best
insight in the whole planning set. But the headline deliverable — a long-lived
SSE status stream — is **solving a problem this single-user app does not have**,
its central architectural decision (stream under `/api/chat/*`) is a real
security/maintenance smell, and several of its load-bearing code claims are
wrong or under-verified. The plan even admits the snapshot+poll path delivers
"~80% of the value"; for this app it's closer to 99%.

---

## 1. Correctness bugs (claims vs. actual code)

### 1a. "CurrentUser on the router" — TRUE, but it makes the design WORSE, not better
Plan D §a.3 says: *"Add to `chat.py` (already has `StreamingResponse`, `CurrentUser`
on router, keepalive pattern)."* Verified: `chat.py:14`
`router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[CurrentUser])`.
So yes, a new `GET /health/stream` on this router inherits bearer auth for free.

But the plan presents this as a *clean* reuse. It is the opposite. The chat
router is the **highest-privilege, write-capable** surface in the app — it owns
`normalize_mode()` whose entire documented invariant (`chat.py:50-56`) is "a
stale/forward-incompatible client must NEVER silently gain WRITE tools." Hanging
a public-ish health endpoint off the same router means every future auth/scope
change to chat (e.g. a per-conversation token, a read-only share scope, rate
limiting on the expensive chat path) now has to reason about a health endpoint
that has nothing to do with chat. That is a maintenance landmine, not a
convenience. **Severity: MEDIUM (design smell, not a bug today).**

### 1b. The Caddy claim is TRUE but the conclusion drawn from it is a hack
Verified `Caddyfile.template:33-37`:
```
@sse path /api/chat/*
reverse_proxy @sse api:8000 { flush_interval -1 }
reverse_proxy api:8000
```
So Plan D is correct: ONLY `/api/chat/*` is unbuffered (`flush_interval -1`); a
new `/api/health/stream` would hit the default `reverse_proxy` and be buffered.
The plan's choice to live under `/api/chat/*` to "inherit the unbuffered proxy
with zero Caddy change" is *factually* sound.

But naming a non-chat health endpoint `/api/chat/health/stream` to dodge a
**one-line Caddyfile edit** is the wrong trade. The Caddyfile is generated from
this template by `install.sh` and re-rendered on update (header comment line 1).
Adding `@sse2 path /api/status/stream` + a second `reverse_proxy` block is a
2-line template change that ships to every operator on the next update —
*exactly* the deploy mechanism the project already uses. Plan D rejects this
("requires every operator to re-run install/edit Caddy") but that's false for
the self-hosted path: the template is the source of truth and updates carry it.
The plan even files the correct fix as "Phase 10, optional." It should be Phase 2
and the `/api/chat/*` hack dropped. **Severity: MEDIUM.**

### 1c. `chat.py` keepalive / header claims — TRUE
Verified: `X-Accel-Buffering: no` + `Cache-Control: no-cache` at `chat.py:183`;
`": keepalive\n\n"` emitted on a 15s `_SSE_KEEPALIVE_SECONDS` timeout
(`chat.py:163-165,19`); pump error path at `chat.py:153-155`. The SSE skeleton
in §a.3 is a faithful copy of the existing pattern. No bug here.

### 1d. api.ts line anchors — PARTLY WRONG
- §0 and §b.2 cite the **~6-per-host warning at `api.ts:728-733`** — verified,
  the comment is at `728-730` and the abort wiring `731-734`. Close enough. ✔
- §b.2 says *"Instrument `streamChat` (`:758-759`)"* — line 758-759 is the
  `reader.read()` / `catch { break }` inside the read loop, **not** a catch block
  you can hang a `health.report()` on cleanly. There is **no outer try/catch**
  around the initial `fetch` in `streamChat` (`api.ts:735-740`) — a network
  failure on the POST itself throws straight to the caller, unwrapped. To feed
  observed health you'd have to *add* a try/catch around the fetch, not
  "instrument the catch block." The plan understates the diff. **Severity: LOW.**
- §b.2 cites `streamSSE` (`:821`) — verified, that's the `catch { break }` at
  `821`. Same issue: it's a per-read break, not an error sink.
- §0/Thesis repeatedly says the health stream "reuses the exact transport
  `streamChat`/`openChatStream` already use." Verified `openChatStream`
  (`api.ts:230-259`) is the closest match (a **GET** fetch+reader). But note
  `openChatStream` sets `credentials: "include"` for the unauth recipient path
  (`:236`) and only sends a bearer when `auth`. The plan's `useHealthStream` is
  authed-only, so it must NOT copy the `credentials:"include"` branch (would be a
  CORS pre-flight surprise with `allow_credentials` OFF). Minor, but the
  "reuse the exact transport" framing papers over a real fork. **Severity: LOW.**

### 1e. Threading model (`call_soon_threadsafe` from worker threads) — SOUND but underspecified
The warmers run via `asyncio.to_thread(embeddings._get_model)` (`main.py:180`,
`211`). So `set_state("embeddings","warming")` would be called **on the event
loop** (before `to_thread`), but `set_state(...,"ready"/"unavailable")` happens
**inside the worker thread** (if placed around `_get_model` itself) OR back on
the loop (if placed in the `_warm_embeddings` coroutine after the `await`).
Plan D §a.2 says *"In `_warm_embeddings`: `set_state("warming")` first, `ready`
after `_get_model`"* — if "after `_get_model`" means after the `await
asyncio.to_thread(...)` returns, that's **on the loop** and `call_soon_threadsafe`
is unnecessary. The plan's registry (§a.1) builds the whole `bind_loop` +
`call_soon_threadsafe` machinery for a cross-thread case that the warmers as
written **don't actually hit**. The cross-thread path only matters for the
observed-LLM downgrade (§a.4) which runs inside `architect.run`'s async context
(also on the loop) — so again, likely no real cross-thread call. **The threading
machinery is probably dead weight.** It's not *wrong* (it's defensive), but the
plan justifies significant complexity (`bind_loop`, a module-global `_loop`,
thread-safe publish) for a hazard that may not exist in the actual call sites.
**Severity: LOW (over-engineering, not a bug). If a worker thread DOES call
`set_state`, then `_publish` MUST use `call_soon_threadsafe` or it'll touch
`asyncio.Queue` from the wrong thread — the plan says it does, so it's safe IF
implemented as described. The risk is that the registry's complexity is unneeded.**

### 1f. The 6-connection-cap concern is largely OBSOLETE for the self-hosted deploy
This is the plan's self-declared "biggest weakness" (§Risks) and it's
**overstated for the primary deploy**. Caddy auto-HTTPS (template `{{DOMAIN}}`
block, comment lines 2-3) serves **HTTP/2** by default. The browser ~6-per-host
TCP-connection cap is an **HTTP/1.1** limit; under HTTP/2 a single connection
multiplexes effectively unlimited concurrent streams. So on the canonical
self-hosted (Caddy) deploy, "chat SSE + status SSE = 2 of 6" is simply wrong —
they're 2 streams on **one** H2 connection, no contention. The cap only bites on
the **cross-origin GitHub-Pages → remote VM** path IF that VM terminates HTTP/1.1
(or a plain dev `vite` proxy). The plan inflates a corner-case into its headline
risk while missing that its *primary* deploy neutralizes it. (Conversely: the
streamChat comment at `api.ts:728-730` that the plan leans on as proof the cap
"already bit" — that symptom was un-aborted **POST** streams piling up, an
HTTP/1.1-era / dev-proxy concern, not evidence the H2 prod path has the problem.)
**Severity: LOW for the claim's danger; MEDIUM as a credibility issue — the plan's
central justification narrative is built on a shaky premise.**

---

## 2. Goal gaps — does the complexity buy anything for THIS app/user?

The goal: a single self-hosted user wants (1) a real server/API health
indicator and (2) pre-flight warnings before using a feature that won't work.

**What actually changes state, and how fast?**
- `embeddings`: `warming→ready` resolves **seconds after boot** (one local model
  load, `main.py:177-202`). After that it never changes for the life of the
  process. A 20s poll catches it within one tick.
- `audio`: same — `warming→ready|unavailable`, once, at boot.
- `llm`: config-derived, **never changes** at runtime except the observed
  downgrade (which is a client-side signal anyway, doesn't need a push).
- `db/push/geocoder`: config-time constants.

So the entire universe of "pushed transitions" Plan D's SSE exists to deliver is:
**embeddings and audio flipping ready, once, ~5s after the server boots.** The
single user is almost never staring at the screen during their own server's cold
boot. The marginal latency win of push-over-poll for this app is **a few seconds,
once per server restart, observed by nobody.** That is the entire payoff of the
SSE stream over Plan A/B/C's poll.

The plan's own §Risks concedes this: *"Snapshot + 15s poll alone delivers ~80%
of the value; the stream is the differentiator."* For a single user it's not 80%
— it's ~99%. The differentiator is a few seconds of latency on a boot transition
no human watches.

**Conclusion:** the observed-health half of Plan D (§b.2, the genuinely good
idea) needs **zero** SSE — it's pure client instrumentation of `api()`/`streamChat`
outcomes and works identically with a poll. The pushed-state half is the only
thing that needs SSE, and it's the half with negligible value for one user.

---

## 3. Risk & robustness

| # | Risk | Severity | Notes |
|---|---|---|---|
| R1 | **SSE under `/api/chat/*` couples health auth/scope to the highest-privilege write router** | MEDIUM | §1a. Future chat scoping (shares, rate-limit, read-only tokens) must now special-case health. Security-relevant blast radius. |
| R2 | **Observed-health false positives** | MEDIUM | A single transient 5xx flips the dot amber and (§a.4) downgrades server-side `llm` to `degraded` for ALL tabs/sessions. Server-global state mutated by one client's unlucky request. The 30s decay helps the client; the **server-side** downgrade in §a.4 is shared mutable state with no per-cause TTL described → one rate-limit blip shows "AI errored" to everyone until the next success. |
| R3 | **Non-Caddy proxies still buffer** | MEDIUM | nginx default-buffers `proxy_buffering on`; the `/api/chat/*` trick only helps Caddy. Plan correctly mandates a poll fallback, but the "open-but-silent → poll" heuristic (§b.5) is fragile: distinguishing "proxy buffering my keepalives" from "quiet server" within N seconds is exactly the flakiness SSE is notorious for. |
| R4 | **Reconnect storms** | LOW | Capped backoff (1→2→5→15→30s) + always-replay-snapshot is reasonable. Fine. |
| R5 | **Offline-auth invariant** | LOW (handled) | §b.2 keeps 401 throwing unchanged so `App.tsx:106` is untouched; §b.4 closes stream on 401 without clearing key. Verified the invariant lives at `App.tsx:97-107` per research. Plan respects it. ✔ |
| R6 | **Cross-origin SSE** | LOW (handled) | fetch+reader with `Authorization`, `allow_credentials` OFF (`main.py:233-242`, verified). Native `EventSource` correctly rejected (can't set bearer). ✔ But see §1d: must not copy `openChatStream`'s `credentials:"include"` branch. |
| R7 | **Cost / token burn** | LOW (handled) | llm health is config-derived + observed; no model call per poll. ✔ Compliant with §8. |
| R8 | **Keepalive vs watchdog timing** | LOW | Server keepalive 15s (`chat.py:19`), client watchdog 25s (§b.4). Margin is fine. But the existing chat `STALL_MS` is **90s** (`api.ts:752,815`), not the "90s stall watchdog" the research doc calls it at `api.ts:752` — verified 90000ms. The health stream wants a *tighter* 25s watchdog, which is a NEW timing regime, not reuse. Minor inconsistency with the "reuse the transport" framing. |
| R9 | **Multi-tab fan-out of server-side llm state** | LOW-MED | §a.4 makes `llm` state process-global, mutated by any tab's failed turn. With one user this is mostly fine, but a background tab's stale request can flap the foreground tab's indicator. |

---

## 4. What Plan D does BETTER than A/B/C (genuine strengths)

1. **Observed-health from real traffic is the single best idea in the entire
   planning set.** A/B/C all gate purely on *declared* readiness; they'll show
   green while a request is actively 5xx-ing because the poll hasn't fired yet.
   Plan D's insight — `api()`/`streamChat` already witness every 5xx, network
   error, and stall, so feed those into the store — catches failures **between
   polls** and reflects what's *actually* happening to the user's requests. This
   is strictly more truthful than B's 20s heartbeat. **A/B/C should steal this.**
2. **Three-state link distinction done rigorously** (browser-offline vs
   server-unreachable vs subsystem-degraded), with observed signals
   (neterr/stall + "no server byte in 8s") as the discriminator. B has the same
   three states but infers server-unreachable only from a *failed poll*; D's
   stall/neterr feed makes "my server is down vs my Wi-Fi is down" sharper and
   faster.
3. **True push latency** on transitions — real, just nearly worthless here (§2).
   In a *multi-user or ops-dashboard* context this would matter; for one user it
   doesn't.
4. **Honesty.** It's the only plan that explicitly quantifies its own marginal
   value ("~80%") and layers the stream so it can be dropped. Credit for that.

---

## 5. What to STEAL / what to DROP

**Steal (into Plan B, the right base):**
- The **observed-outcomes feed** (§b.2/§b.3): instrument `api()`/`streamChat`
  to `report()` 5xx/neterr/stall into the health store. ~30 lines, no transport,
  huge truthfulness win. This is Plan D's gift to the others.
- The **reconciliation precedence** (§b.3): pushed/declared state wins; observed
  can only *downgrade* transiently and self-heals. Clean model.
- The **observed-LLM downgrade** (§a.4) — but make it **client-side only** (don't
  mutate server-global state per R2), or give the server downgrade a strict
  per-cause TTL.

**Drop (over-engineered for this app):**
- The **SSE stream itself.** For a single self-hosted user the only pushed
  transition is boot-time embeddings/audio readiness, caught by a 20s poll +
  5s-while-warming cadence (Plan A already proposes adaptive cadence,
  `10-plan-A-minimal.md:180-191`). The latency win is unobservable.
- The **`bind_loop`/`call_soon_threadsafe` registry machinery** (§a.1) — likely
  dead weight (§1e); a plain dict + a `threading.Lock` (Plan B's `_state_lock`)
  suffices since the warmer state writes are on-loop or trivially lockable.
- The **`/api/chat/health/*` placement** — replace with a proper `/api/system/status`
  (Plan B) and, if you ever do SSE, a 2-line Caddy `@sse` rule, not a router hack.
- The **open-but-silent → poll-fallback heuristic** (§b.5) — only needed because
  you chose SSE; deleting SSE deletes the need to detect proxy buffering.

---

## 6. Verdict

Plan D diagnoses the problem better than anyone and then builds a transport the
problem doesn't require. Its **observed-health bus is the best idea in the
planning exercise and must survive**; its **SSE stream is a solution to a
multi-user/low-latency problem that a single self-hosted user does not have.**
The `/api/chat/*` placement trades a clean 2-line Caddy edit for a permanent
security/maintenance smell on the most privileged router. Recommendation:
**adopt Plan B as the base, transplant Plan D's observed-outcomes feed and
reconciliation into it, and shelve the SSE stream** (it's cleanly droppable, by
the author's own design).

**Is the SSE stream worth it for this single-user app? No.** Poll (20s steady /
5s while-warming) plus Plan D's observed-traffic instrumentation gives a more
truthful, lower-complexity indicator. SSE adds a long-lived socket, a registry
with cross-thread machinery of dubious necessity, a proxy-buffering failure mode,
a poll fallback to detect that failure mode, and a router-coupling smell — to win
a few seconds of latency on a boot transition no human watches. Keep the stream
on the shelf as a documented future option if the deployment ever goes multi-user.

### Top 5 must-fix (ranked)
1. **Drop the SSE stream as the primary mechanism**; ship snapshot + adaptive
   poll. Layer SSE later only if multi-user. (Author already made it droppable —
   take the off-ramp.)
2. **Move the endpoint off `/api/chat/*`** to `/api/system/status` (+ a 2-line
   Caddy `@sse` rule if SSE is ever added). Stop coupling health to the
   write-capable chat router. (§1a/§1b)
3. **Make the observed-LLM downgrade client-side** (or strictly TTL'd
   server-side) so one tab's transient 5xx doesn't flip global `llm` state for
   every session. (§R2)
4. **Keep the observed-outcomes feed** (§b.2/b.3) — and fix the anchors: there's
   no outer try/catch around `streamChat`'s initial fetch (`api.ts:735-740`); you
   must add one, not "instrument the existing catch." (§1d)
5. **Delete the `bind_loop`/`call_soon_threadsafe` registry** unless a concrete
   worker-thread `set_state` call site is identified; warmer state transitions
   land on the event loop. A `Lock`-guarded dict is enough. (§1e)
