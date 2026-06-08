# Red-Team 1 — Critique of Plan A ("Minimal extension, lowest-risk diff")

**Reviewer role:** adversarial. **Verdict up front:** Plan A is the most
*shippable* of the four and its Phase-0 fact-check is the most honest, but it
under-delivers on the literal goal ("real-time", "warn before use") in three
concrete ways, ships one genuine readiness **bug** (audio `_model_key` reload),
and contains a UX/semantics defect (a 401 during polling is mislabeled as
"server unreachable"). It is a strong skeleton, not a finished plan.

---

## 1. Correctness bugs (verified against code)

### 1.1 [MEDIUM] Audio readiness is wrong under model-config reload
Plan A (§1b) wraps `audio_transcription._get_model()` to set `ready` once on
`WhisperModel(...)` success. But the real `_get_model()`
(`server/app/services/audio_transcription.py:93-110`) **reloads** whenever the
runtime-editable settings change:

```python
want = (audio_model(), audio_compute_type())
if _model is None or _model_key != want:      # line 98
    with _model_lock:
        if _model is None or _model_key != want:
            ...
            _model = WhisperModel(want[0], device="cpu", compute_type=want[1])
            _model_key = want
```

These are DB-`meta`-overridable at runtime (`audio_model()` line 46-48,
`audio_compute_type()` line 51-53) — the docstring explicitly says "Reloads if
the configured model/compute_type has changed (e.g. edited in the Settings
GUI)." Plan A's state machine has no notion of `_model_key`, so after an owner
changes the model in Settings the cached state stays `ready` while the next
`_get_model` is actually re-downloading (multi-hundred-MB). The dot says green;
the feature blocks. Plan A's own claim that "`_warm_audio` already calls
`_get_model` so transitions happen for free" is therefore only true for the
*first* load. The wrap must key off `want`/`_model_key`, not a one-shot flag.
(The same is **not** a problem for embeddings, which never reload.)

### 1.2 [MEDIUM] Poller mislabels 401 as "unreachable"
Plan A §3a's `useHealth` calls `get("/api/status")`, and `get()` →
`api()` throws `ApiError("Not authenticated", 401)` for any 401
(`web/src/api.ts:45`). The catch sets `server = "unreachable"` for *all*
errors including 401. So a rotated/expired key makes the dot go **red
"server unreachable"** — actively misleading (server is fine; auth is dead).
Plan A is right that it must not call `clearAccessKey` (good — preserves the
`App.tsx:106` invariant), but it should branch on `e.status === 401` to show a
distinct "re-authenticate" state, and arguably let App's existing verify path
own logout. As written it conflates two very different failures, which is
ironic given the plan's headline is "distinguish server-unreachable from
browser-offline."

### 1.3 [LOW] Hook dependency array causes request churn
The `useEffect` deps are `[authed, caps?.embeddings.state, caps?.transcription.state]`
(§3a line 186). Every state transition (idle→warming→ready) tears down the
effect, fires a fresh immediate `tick()`, and rebuilds the interval. During a
normal cold boot that's 2 subsystems × (idle→warming→ready) = up to ~4 extra
immediate fetches plus interval thrash. Minor, but it contradicts the "cheap &
frequent" selling point and the adaptive-cadence design (the interval is reset
mid-warm anyway). Cleaner: keep cadence in a ref and don't re-subscribe on
every state byte.

### 1.4 [LOW] Line-anchor / claim nits (mostly accurate)
Plan A's Phase-0 is unusually accurate. Confirmed correct: `/api/health`
`main.py:254-256`; `/verify` `auth_router.py:22-38`; embeddings `_model`/
`_model_lock` `embeddings.py:16-17`, `_get_model` `:20-30`; audio
`:38-40,93-110`; warmups `main.py:177-202,208-215`; `AuthCtx`/`useAuth`
inline at `App.tsx:47-48`; boot verify and offline-tolerant catch at
`App.tsx:101,106`; `useOnline` `hooks.ts:264-277`; CORS expose-headers
`main.py:241`; Attachments gating `hasLlm` at `:38,290`. Two small misses:
- Plan A cites the ReviewBell resume pattern as `Shell.tsx:42-51`; the actual
  visibility/focus handlers are `Shell.tsx:42,49-52` (with `pageshow` too at
  `:51` — Plan A's hook omits `pageshow`, which the codebase deliberately adds
  "because mobile/PWA pause setInterval while backgrounded" `Shell.tsx:38-39`).
  Add `pageshow`.
- Plan A says `/verify` "also calls `people.owner_name(get_conn())` and
  `push.public_key()`" — correct (`auth_router.py:27-38`), good justification
  for a separate cheaper `/status`.

### 1.5 [LOW] `embeddings.readiness()` won't catch a hard import failure as `failed` early
fastembed import lives *inside* `_get_model` (`embeddings.py:25`). Plan A's
wrap sets `warming` then `failed` on exception — fine — but only on first call.
The warm task swallows the exception (`main.py:200-201`), so `failed` is only
ever observed if `_warm_embeddings` runs (it does). OK in practice, but note the
state is `idle` until the warm task's `to_thread(_get_model)` actually enters —
there's a window where the UI shows `idle` and Plan A never defines UI copy for
`idle` distinct from `warming`. Plan B/C explicitly handle this (`unknown`).

---

## 2. Goal gaps — does it actually meet "real-time" + "warn before use"?

### 2.1 [HIGH] "Real-time" is 20s polling — and the plan admits it
The owner asked for **real-time**. Plan A delivers 5s-while-warming / 20s-steady
polling and honestly flags this (§10.1). That's defensible for cost, but the
warming window (the *only* moment real-time matters for embeddings/audio)
resolves in seconds after boot — so a user who opens the app mid-warm sees a
red/amber dot that may lag the true ready state by up to 5s, and steady-state
subsystem death (e.g. disk fills, model evicted) lags up to 20s. Plan D's
observed-outcomes feed (instrument `api()`/`streamChat` which *already* see every
5xx/stall) is the cheap way to get true immediacy with zero new transport — Plan
A ignores this entirely and leaves a real gap between polls.

### 2.2 [HIGH] Coverage of subsystems is too narrow for "any service that won't work"
Plan A's manifest covers exactly **three** things: llm, embeddings,
transcription. The goal says *any* service. Verified gaps vs. the research doc
(§1-2) and code:
- **push / VAPID** — `push.public_key()` already exists and `/verify` returns
  it; gating push-subscribe is trivial and Plan A drops it. (Plan B & C both
  cover it.)
- **geocoder** — `geocode.enabled()`/`geocoder_url` exists (`config.py`), drives
  Map address labels; not covered.
- **scheduler liveness** — `_scheduler_loop` swallows per-iteration errors
  (`main.py`); a wedged loop silently kills workflows/triggers/trip-detection
  with zero signal. Plan B's scheduler heartbeat catches this; Plan A can't.
- **db** — Plan A's `/status` does "no DB" by design, so it never actually
  proves the DB is writable; "reachable" only means the process answered. For a
  health indicator this is a meaningful blind spot (a read-only/locked WAL is
  invisible).
- **entity_rebuild / image-analysis** — already have status endpoints
  (`entities/status`, `attachments/{id}/analysis-status`) but aren't folded into
  the unified signal.

### 2.3 [MEDIUM] LLM "configured" can't warn before use in the case that matters most
Plan A (correctly, for cost) reports `configured` vs `absent` and never
live-checks (§1c, §8). But the highest-frequency real failure is a *present but
revoked/over-quota* key — which shows **green** and fails at call time. Plan A's
only backstop is "the in-the-moment error" (§6, §10.2). Plan B and D both add a
zero-cost improvement Plan A omits: record the **last real provider error**
(`llm._last_auth_error`, set when an actual completion 401s/billing-fails) and
surface `degraded`. That's free (no extra tokens, piggybacks real traffic) and
directly serves "warn before use" after the first failure. Plan A should steal
it.

### 2.4 [MEDIUM] Gating breadth is a hand-wave
§5b is a table, not a plan. It says "Chat send", "Rebuild / Research / Labs AI",
"respective pages" without anchors, where Plan C did the *exhaustive* inventory
with real line numbers (`Chat.tsx:537-575,669,946-947`, `SearchPage.tsx:36`,
`AiAnalysisPanel.tsx:27-40`, `RebuildPanel.tsx:5-9`, `AdvancedHome.tsx:10-42`,
`ModelPicker.tsx:30-66`). Plan A's "principle: degrade where a fallback exists"
is the right *philosophy*, but as an implementation spec it will silently miss
surfaces — exactly the drift risk it lists in §10.6 without mitigating it.
Notably it doesn't mention generalizing `ModelPicker.tsx`'s existing private
`/verify` re-fetch (`ModelPicker.tsx:37`) — a duplicate poll that should be
folded into the new context.

### 2.5 [MEDIUM] Error surfacing is the weakest section
§6 keeps the blocking `alert()` pattern and only "promotes silent catches near
gated actions." The research doc (§5, §7.3) calls the silent `.catch(()=>{})`
and blocking `alert()` UX a core gap. Plan A essentially declines to fix it
("No toast library"). B, C, and D all add a ~80-line dependency-free toast —
that's cheap and squarely on-goal ("errors aren't verbose/visible enough in the
moment"). Plan A's refusal here is the clearest case of "minimal" undershooting
the brief.

---

## 3. Risk & robustness

| # | Finding | Severity |
|---|---|---|
| 3.1 | **Multi-worker flicker.** Per-process in-memory state means readiness diverges across workers. Verified **not a problem today**: `Dockerfile:45` runs `uvicorn app.main:app` with **no `--workers`**, and `llm.py:36` comment confirms "the single uvicorn worker." Plan A correctly flags this as latent (§10.3). Accept as-is but document the constraint loudly so nobody adds `--workers` later without a shared store. | LOW |
| 3.2 | **401-as-unreachable** (see §1.2) — security-adjacent UX bug; could mask a rotated key as an outage and delay re-auth. | MEDIUM |
| 3.3 | **`last_error` leak.** Plan A truncates to `[:200]` and gates behind `CurrentUser` (good). But embeddings/audio `last_error` can contain filesystem paths / HF URLs / OOM traces. Post-auth only, so acceptable, but confirm it's never echoed into the public `/api/health` or `/auth/info`. As specified it isn't — OK. | LOW |
| 3.4 | **Cross-origin: fine.** Poll uses `get()`→`u()`+bearer, body-only JSON; doesn't depend on `expose_headers` (`main.py:241`). Verified safe. | — |
| 3.5 | **Cost: fine.** `/status` does no DB/model/network; `verified:null`. Genuinely cheap. The one wart: §1.3 churn adds a few redundant fetches at boot. | LOW |
| 3.6 | **Race between gate and click** — acknowledged (§10.4); the only backstop is in-the-moment errors, which §6 under-builds. Compounds with §2.5. | MEDIUM |
| 3.7 | **No AbortController/timeout on the poll.** Unlike Plan B/D (8s abort), Plan A's `get()` has no timeout; a hung TCP connection to a dead VM leaves the poll pending indefinitely and the dot never flips to red until the browser's own TCP timeout (can be 30-120s). This directly undermines "real-time server health." Add a timeout. | MEDIUM |

---

## 4. What Plan A does BETTER than B/C/D (keep these)

1. **Most accurate, lowest-friction grounding.** Phase 0 verified almost
   perfectly against code; it correctly identifies that `AuthContext.tsx`
   doesn't exist (it's inline `AuthCtx` in `App.tsx:47`) — a trap B and C
   gloss (C even calls it "AuthContext").
2. **Piggybacks the initial snapshot on the existing boot `/verify`** (§2b) so
   the first capability paint costs **zero** extra round-trips. Neither B
   (separate `system/status`) nor D (separate stream) gets the first paint as
   cheaply.
3. **Smallest, safest backend diff** — two ~10-line state wraps + one assembler
   + one route. Plan B adds a whole `system_status.py` aggregator + soft-auth
   router + scheduler heartbeat writes; Plan D adds a registry + SSE under
   `/api/chat/*`. Plan A is the only one that's trivially reviewable.
4. **Explicitly avoids token burn** with the clean `configured`/`absent` +
   `verified:null` shape — same posture as B/C but stated most crisply.
5. **Correctly refuses SSE/WS** given the verified browser 6-connection cap that
   already bit `streamChat` (`api.ts:728-733`) — the exact risk Plan D shoulders.
   Plan A's instinct here is right.
6. **Adaptive cadence (5s warming / 20s steady)** is a nice, cheap nod to
   real-time during the only window it matters.

## 5. What Plan A should STEAL from B/C/D

- **From B:** the `llm._last_auth_error` opportunistic degrade (zero-cost
  validity signal); the scheduler heartbeat (`set_meta("scheduler:last_beat")`);
  the **8s AbortController timeout** on the poll; and the broader subsystem set
  (push, geocoder, db SELECT 1). Also B's `unknown` state for the pre-first-poll
  window Plan A leaves undefined (§1.5).
- **From C:** the **exhaustive feature→capability inventory with real anchors**
  (Plan A §5b is too thin), and folding `ModelPicker.tsx`'s private `/verify`
  fetch into the shared context. C's `warming` vs `unavailable` copy distinction
  is also sharper than A's.
- **From D:** the **observed-outcomes feed** — instrument the existing central
  `api()` (`api.ts:40-57`) and `streamChat` to mark the server suspect on
  5xx/neterr/stall between polls. This is the cheapest path to genuine "real-time"
  and to closing the gate-vs-click race (§3.6). And D's dependency-free toast bus.

## 6. Verdict

Plan A is the right **chassis** — accurate, minimal, cheap, offline-safe — but
as written it answers "lowest-risk diff" more than it answers "real-time health
+ warn before use." It is too narrow (3 subsystems), too polling-bound, and
punts the error-surfacing the brief explicitly calls out. The best outcome is a
**hybrid**: Plan A's backend skeleton + B's broader manifest & last-auth-error &
timeout + C's exhaustive gating + D's observed-outcomes feed.

### Top 5 must-fix, ranked
1. **[HIGH] Broaden the manifest** beyond llm/embeddings/transcription to
   push, geocoder, scheduler-heartbeat, and a DB `SELECT 1` — otherwise it
   doesn't meet "*any* service that won't work" (§2.2).
2. **[HIGH] Close the between-polls gap** with D's observed-outcomes feed
   (instrument `api()`/`streamChat`) so failures surface in real time and the
   gate-vs-click race is covered (§2.1, §3.6).
3. **[MEDIUM] Fix the audio readiness bug** — key state off `_model_key`/`want`,
   not a one-shot flag, so a Settings-driven model reload isn't shown as green
   (§1.1).
4. **[MEDIUM] Disambiguate 401 vs unreachable** in `useHealth`, and add an
   AbortController timeout so a dead VM flips the dot promptly (§1.2, §3.7).
5. **[MEDIUM] Actually build error surfacing + the gating inventory** — adopt
   the ~80-line toast and C's anchored feature table; record `llm` last-auth-error
   for present-but-invalid keys (§2.3, §2.4, §2.5).
