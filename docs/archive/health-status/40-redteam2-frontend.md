# Red Team 2 — Comparative FRONTEND critique (v2 plans, converged)

**Reviewer:** Red Team (round 2) · **Date:** 2026-06-08 · **Scope:** frontend only,
verified against `web/src/...` and the backend anchors the frontend depends on.

All four v2 plans have converged on the same skeleton: a cheap polled status
endpoint + adaptive cadence + a client observed-outcomes feed + a status dot +
a capability gating inventory + an ~80-line toast. The remaining differences are
in the *details that decide whether it compiles, lies, or logs you out*. This
review hunts those, per the seven focus areas, then synthesizes the single best
convergent design.

Verification legend: ✅ verified against code · ⚠️ caveat/risk · ❌ wrong.

---

## 1. The status fetch helper

All four correctly recognize `authHeaders` is module-private (`api.ts:34` ✅) and
`api()` throws `ApiError(...,401)` on 401 (`api.ts:45` ✅), so a poller cannot
reuse `api()` without risking the logout path. `u()` is exported (`api.ts:30` ✅);
`getAccessKey()` is exported (`api.ts:14` ✅); `clearAccessKey()` exported
(`api.ts:24` ✅).

| Plan | Helper | 401 vs unreachable | Timeout | Cross-origin | Verdict |
|---|---|---|---|---|---|
| A | `getStatus()` exported from api.ts, reuses **private** `authHeaders()`+`u()` internally; returns a discriminated `{ok:true,data}` \| `{ok:false,reason:"needs-auth"\|"unreachable"}` | ✅ `res.status===401`→`needs-auth`; `!res.ok`→`unreachable`; `catch`→`unreachable` | ✅ 8s AbortController, abort→unreachable | ✅ uses `u()`+`authHeaders()` | **Cleanest.** |
| B | `fetchStatus(path, signal)` exported, builds headers from `getAccessKey()`, returns `{status, body}` (raw) | ⚠️ route is **soft-auth** so a 401 "can't happen" — caller maps `status>=500`→unreachable; but body parsing/`status===0` handling pushed to caller | ⚠️ timeout owned by the hook (8s), not the helper | ✅ `u()`+`getAccessKey()` | Good, but leaks classification logic to the caller. |
| C | reuses `u()`/`authHeaders` via the provider's poll loop; "bubbles 401 to the App handler" | ❌ **ambiguous** — text says "bubbles 401" yet also "never clears auth"; no concrete non-throwing helper shown. If it literally reuses `api()`, a poll 401 throws into the provider, not App.tsx. | ✅ 8s abort | ✅ | **Underspecified — must adopt A's/B's explicit helper.** |
| D | "export `authHeaders` (one line)" + raw `fetch(u(...))` in the store | ⚠️ Works, but **exporting `authHeaders` is the weakest choice** — it re-opens the door to ad-hoc authed fetches elsewhere, the exact thing the private symbol prevents. A's "export one purpose-built helper, keep `authHeaders` private" is strictly better hygiene. | ✅ 8s abort | ✅ | Functional but worse encapsulation. |

**Findings:**
- **[MED] Plan C's helper is underspecified** and risks routing a poll-401 through
  the throw path. Must take A's `getStatus()` or B's `fetchStatus()`.
- **[LOW] Plan D's "export `authHeaders`"** regresses encapsulation vs A's
  purpose-built export. Reject; keep `authHeaders` private.
- **[LOW] B's soft-auth means 401 is unreachable-by-construction**, which is
  elegant *only if* the status route is soft-auth (B/D). For A/C's key-gated
  routes, the explicit `needs-auth` state is the right model — and it is genuinely
  useful UX (rotated key → amber "Re-authenticate", not red "unreachable"). **A's
  explicit `needs-auth` state is the most informative of the four.**

**Winner: A's `getStatus()`** — keeps `authHeaders` private, distinguishes
`needs-auth`/`unreachable` cleanly, owns its own 8s timeout, never calls
`clearAccessKey`.

---

## 2. Observed-health feed

All four converged on instrumenting the central wrappers — the single best idea
in the exercise — because they already witness every outcome. Anchors verified:
- `api()` `api.ts:40-57` has **no try/catch**; network errors throw raw ✅
- `streamChat` initial `fetch` at `api.ts:735-740` is **OUTSIDE** the read-loop
  `try` (which starts at `:755`) ✅ — so D/B/C are right that try/catch must be
  *added*, not "instrumented." **Plan A's §3c wording ("instrument the central
  api() and streamChat") is loose** but its Phase-8 steps and risk text show it
  understands a wrap is needed; still, A should explicitly say "add try/catch
  around the `:735` fetch" like B/C/D do. **[LOW]**
- `streamSSE` initial fetch at `api.ts:806-810`, per-read `catch{break}` at `:821`,
  `STALL_MS=90000` at `:752`/`:815` ✅

| Plan | Bus | Self-heal | Store location | Re-render risk |
|---|---|---|---|---|
| A | `health-observed.ts` ~40-line module bus; `drainObserved`/`onObserved`; `applyObserved` merges onto polled snapshot (observed can only **downgrade**) | ✅ `llm-fail`≤60s w/ no newer `llm-ok`→`degraded`; next `llm-ok`/poll re-asserts | Caps in `useHealth` `useState`, fed into `AuthCtx` | ⚠️ merging into AuthCtx re-renders the whole authed tree on every poll/observed tick |
| B | observed fields inside the heartbeat singleton store; "declared wins, observed only downgrades" | ✅ 60s LLM decay, 30s 5xx decay | **module singleton** + `useHeartbeat` (good) but exposed via `StatusCtx` | ⚠️ context consumers re-render |
| C | `health.ts` singleton bus; declared-wins precedence | ✅ self-heal on next poll/turn | provider context | ⚠️ context |
| D | `health.ts` singleton + **`useSyncExternalStore`** | ✅ client-only LLM downgrade, self-heals | **module singleton, `useSyncExternalStore`** | ✅ **best** — `useSyncExternalStore` lets each consumer subscribe to a selector; no provider-wide re-render |

**Findings:**
- **[MED] False-positive risk is real in all four but bounded.** A single transient
  5xx flips the dot. All four mitigate with short decay (30s) + declared-state
  precedence + "observed only downgrades." Adequate, but **the dot must require
  *either* a failed poll *or* a sustained observed signal before going red** —
  not a single 5xx → red. B/D state this most clearly ("recent neterr/stall AND
  no server byte within ~8s"). A's `applyObserved` "5xx+failed-poll → immediate
  unreachable" is the correct conjunction. **C is vaguest here.**
- **[MED — re-render] Store-in-context (A/B/C) re-renders broadly.** A threads caps
  through `AuthCtx`, so every authed component re-renders each 20s poll and each
  observed tick. **D's `useSyncExternalStore` singleton is the only design that
  avoids re-render storms** by construction — consumers subscribe to slices.
  **This is D's standout frontend contribution.**
- **[LOW] LLM-ok signal:** only A explicitly emits `llm-ok` on a clean LLM turn to
  self-heal faster than the next poll. Nice; cheap; worth keeping.

**Winner: D's `useSyncExternalStore` singleton store** + A's explicit
`applyObserved` downgrade/self-heal rules (incl. `llm-ok`) + B/D's
"neterr/stall AND no-byte-within-8s" conjunction for `unreachable`.

---

## 3. Offline-tolerant auth invariant — CRITICAL

`App.tsx:106` verified exactly: `.catch((e) => { if (e?.status === 401)
clearAccessKey(); else setAuthed(true); })` ✅ — only a real 401 from
`/api/auth/verify` clears the key; everything else keeps the user authed.

| Plan | Poller touches `clearAccessKey`? | Observed feed preserves `api()` 401-throw? | Verdict |
|---|---|---|---|
| A | ❌ never; `getStatus()` returns `needs-auth`, only flips the dot; explicit "App.tsx:106 owns logout" | ✅ "401 still throws unchanged" | ✅ Safe |
| B | ❌ never; soft-auth route can't 401; explicit spy test | ✅ "401 still throws unchanged" | ✅ Safe |
| C | ⚠️ says "bubbles 401 to the App handler" **and** "never clears auth" — contradiction; if the poll reuses `api()`, a poll-401 throws into the provider's `.catch`, which must NOT call `clearAccessKey`. **Not proven safe as written.** | ✅ instruments but keeps 401 throw | ⚠️ **must pin down** |
| D | ❌ never; "401 only flips `link`; logout stays at App.tsx:106" | ✅ explicit | ✅ Safe |

**Findings:**
- **[HIGH] Plan C is the only one whose poller is not provably safe.** Its
  "bubbles 401 to the App handler" language implies the poll could feed the auth
  path. The hybrid MUST use a non-throwing helper (A/B) so a poll 401 is
  *impossible* to route into logout. With A's `getStatus()` this is safe by
  construction.
- **[LOW] Note** `App.tsx:106` keeps you authed on *any* non-401 including a 403 or
  malformed response. The observed feed must not treat a 403 as a reason to log
  out either — none of the plans do; good.

**Winner: A and D are both provably safe.** Mandate a non-throwing poll helper +
keep `api()`'s 401-throw intact. Reject C's ambiguous "bubble 401" wording.

---

## 4. Where the provider mounts

Verified: `AuthCtx.Provider` wraps `<Routes>` (`App.tsx:121`); `/share/:token` →
`<SharePage/>` is **inside** the provider tree but **before** the auth gate
(`App.tsx:124`), rendered with no Shell, no key; `!authed ? <KeyEntry/>` at
`:127`; `<Shell>` (authed) at `:129`. ✅

| Plan | Mount | Pre-auth reachability on KeyEntry | Reconciles `/share/:token`? |
|---|---|---|---|
| A | StatusDot in Shell (authed only) | ❌ none | N/A (no pre-auth poll) |
| B | **StatusProvider above the auth gate**; indicator in Shell; minimal reachability line on KeyEntry | ✅ genuine win: distinguish "server down" vs "you're offline" before login | ⚠️ see below |
| C | provider **inside** authed branch (wraps Shell) | ❌ none | ✅ explicitly scopes share OUT (server-driven, §6) |
| D | poll started in authed App; soft-auth public skeleton callable on KeyEntry | ◐ design supports it, mount not pinned | — |

**Findings:**
- **[MED] B's "above the auth gate" mount is the right call for the stated goal.**
  The brief is "tell me before I try" — and the most basic case is *"is the server
  even reachable"* at the login screen. Today KeyEntry shows nothing. B's minimal
  reachability line (no capability detail pre-auth) is a real UX win and a clean
  security posture (skeleton only).
- **[MED — must-fix for the hybrid] The `/share/:token` route is mounted inside the
  provider tree but is PUBLIC and unauthed.** If the hybrid mounts a
  StatusProvider above the auth gate (B), it will also wrap SharePage. The
  provider's poller hits a **key-gated** status route (A's `/api/auth/status`,
  C's `/api/capabilities`) → 401/`needs-auth` for every share recipient, firing a
  pointless poll on a stranger's device. **Mitigation: either (a) use a soft-auth
  status route (B/D) that returns a public skeleton, OR (b) don't mount the
  poller on the `/share/:token` branch.** Cleanest: the StatusProvider wraps the
  `path="*"` element only (auth + Shell tree), NOT the share route — give the
  share route its own minimal tree. B's plan says "wraps the whole `<Routes>`
  tree," which would wrap SharePage; **this needs the carve-out.** **[MED]**

**Winner: B's above-the-gate mount, with a mandatory carve-out so the poller does
NOT run on `/share/:token`.** Pair with a soft-auth route so even a stray call
returns a harmless skeleton.

---

## 5. Gating inventory — spot-check of Plan C v2 (the most exhaustive)

Spot-checked 7 rows against code:

| C v2 claim | Code | Verdict |
|---|---|---|
| Search modes `["hybrid","keyword","semantic","entities"]` at `SearchPage.tsx:36`, default hybrid `:44`, swallowed catch `:79`, query-on-keystroke | `SearchPage.tsx:6,36,43-44,77,79` | ✅ exact |
| **search.py:80-92 semantic calls are BARE (no try/except)** — hybrid/semantic hangs on warmup or 500s; NOT a silent FTS fallback | `search.py:80-92` confirmed bare; keyword `:37-48`, entity-semantic `:94-103` ARE wrapped | ✅ **C is right; A is WRONG** (see below) |
| GuidedChat/ResearchChat rendered **only by SharePage** (`SharePage.tsx:67,73`), outside provider, via `getShare`/`publicApi` | `SharePage.tsx:5,9-10,52,66-69,72-74` | ✅ exact |
| Labs button is **"Extract lab values"/"Re-analyze"** (`reanalyzeLabs`), no "AI import" button | `LabImportPanel.tsx:58-59,82,165` | ✅ exact |
| Entity rebuild is **deferred/coalesced** via merge/split (no standalone "Rebuild all" button); needs llm+embeddings | `entities.py:40-66` (`request_rebuild` after merge), `EntitiesPage.tsx:66-77` polls `/status` | ✅ exact |
| Video transcribe → vision summary gated server-side on LLM key | `audio_transcription.py:251-252` ("Needs an LLM key"); `:160` `VIDEO_FRAME_MAX=0`→off | ✅ exact |
| Attachments: help copy `:197`, Transcribe `:285` (ungated), Analyze `hasLlm && isImage` `:290` | `Attachments.tsx:197,285,290,38` | ✅ exact |
| Map geocoder gate is a near-no-op (labels pre-resolved `location_label`) | `MapPage.tsx:212` — labels come pre-resolved; no client geocode call found | ✅ C's removal is correct |
| ModelPicker private `/verify` re-fetch for `llm_keys` | `ModelPicker.tsx:37,48-52,61` | ✅ exact |

**C v2's inventory is the most accurate of the four and survives spot-checking.**

**Cross-plan gating defects this exposes:**
- **[HIGH] Plan A's §5c Search rows are wrong.** A claims hybrid is "Allowed while
  `warming` (server falls back to FTS)" and "capture still re-indexes." The server
  does **NOT** fall back — `search.py:80-92` is bare. A hybrid/semantic query while
  embeddings warm will **block on `_get_model` under the lock or 500**, fired on
  *every keystroke* (SearchPage queries on each keystroke, `:77`). A's design rests
  on a fallback that does not exist. **The hybrid MUST take C's a0 backend fix
  (wrap the two semantic calls) AND C's "force keyword while not ready" gate.**
- **[MED] Only C fixes the real backend bug (a0).** B references it as "optional
  hardening (§A1n)"; D references it in a gating note but leaves search.py
  unwrapped; A ignores it. **a0 is not optional — it is load-bearing for every
  plan's search gating to be truthful.**
- **[MED] Plan B mis-files two rows** that C corrected: B still lists "Owner-assisted
  chat route needs llmReady" (`App.tsx:138` OwnerChatPage) — but C verified
  `OwnerChatPage` is **E2EE human-to-human**, not LLM (`share.py:527-548`). B's
  `OwnerChatPage` llm-gate is a **wrong/no-op gate**. B also lists a Map geocoder
  gate C proved is a no-op.

**Missed entry points (across all four):** I scanned `App.tsx:132-156` routes.
- `WorkflowsPage`/`ActionsPage` (`/flows`, `/actions`): C notes "per-trigger note
  when its action needs a missing cap" but no anchor; A/B/D omit. **[LOW]** — these
  are config editors; run-time LLM need is genuinely deferred, so a soft note is
  fine, but none pin it down.
- `OwnerOnboarding` (`App.tsx:128`) renders **before** Shell when `!ownerSet` — no
  plan gates anything there, correct (no AI surface), but **note the status dot is
  invisible during onboarding** since it lives in Shell. **[LOW]** acceptable.
- No plan addresses `ReviewPage`/`NotificationHistoryPage` — neither has an AI
  surface; correctly omitted.

**Winner: C's inventory wholesale**, including the a0 backend fix and the
corrections to B's wrong OwnerChat/Map gates.

---

## 6. Public-share pre-flight gating (Plan C v2)

Verified: `share_read` (`share.py:108`) dispatches to `_research_landing`
(`:261`)/`_guided_landing` (`:163`); `llm_ready()` exists (`:197-199` →
`llm.has_credentials()`); `_resolve_guided` already 404s on `not llm_ready()`
(`:192`); SharePage reads the landing via `getShare`/`publicApi` (`:52`) and
renders GuidedChat/ResearchChat (`:66-74`). ✅

**Findings:**
- **[Sound] C's server-driven `llm_ready` landing flag is the right and only
  honest design.** The public route has no auth context and structurally cannot
  read the capability manifest (GuidedChat/ResearchChat don't import `useAuth`).
  Adding one already-computed boolean to the landing payload and rendering a clear
  "temporarily unavailable" instead of letting the recipient hit a generic 404 is
  correct, cheap, and leaks nothing the recipient wouldn't learn by trying.
- **[LOW] Caveat:** `llm_ready()` = `has_credentials()` = key *present*, not valid.
  A revoked key passes the landing flag, then the chat 404s at `start`/`turn`.
  Acceptable (same cost rule as everywhere); the existing 404 backstop covers it.
  The recipient's UX could still confusingly degrade mid-chat, but that's an
  irreducible cost-vs-correctness tradeoff.
- **[LOW] One gap:** C adds the flag to guided/research landings but the
  *research* landing is the one whose 404 also reads as "revoked." Confirm the
  flag is added to **both** `_guided_landing` AND `_research_landing` (C says so;
  ensure the implementation does both — `_research_landing:261` is verified to
  exist).

**Winner: C's approach as-is.** The other three correctly scope public share OUT
of the client manifest; C is the only one that gives it a real pre-flight signal.

---

## 7. Toast / error surfacing

All four propose an ~80-line dependency-free toast replacing blocking `alert()`
in Chat/NotePage/Attachments. Differences:

| Plan | De-dup | Classification | Re-render |
|---|---|---|---|
| A | observed bus de-dupes a 5xx burst → one dot + one toast; `explainError` consults live caps | via `CAP_COPY` | toast state local to `Toaster` |
| B | shares `ApiError.category` with the heartbeat so toast + dot agree | `ApiError.category` ("auth"\|"network"\|...) inferred from status | local |
| C | fed by same `health.ts` bus; `explainError(err, capHint)`; **explicitly excludes SearchPage `:79`** to avoid keystroke-rate toast storm | `ApiError.category` + `explainError` | local |
| D | toast fed by `health.report({kind:"error"})`; 5xx burst → one de-duped toast | bus-driven | `useSyncExternalStore` |

**Findings:**
- **[MED] SearchPage keystroke toast storm is a real trap** — SearchPage queries on
  every keystroke (`:77`) and swallows errors (`:79`). **Only C explicitly calls
  this out and excludes `:79` from the toast layer.** A/B/D would risk a
  toast-per-keystroke on a cold boot if a naive "route all catches through toast"
  rule were applied. The hybrid MUST keep C's exclusion.
- **[LOW] `ApiError.category` (B/C)** is cleaner than ad-hoc tags and lets the toast
  and dot agree on classification. Worth adopting; it's a 5-line addition in the
  existing `api.ts:46-53` parse block.
- **[LOW] Promote silent `.catch(()=>{})` loaders to quiet toasts only when
  `serverHealth==="ok"`** (A states this) — otherwise a real outage = toast storm.
  Good rule; adopt.

**Winner: C's bus-fed toast + `explainError` + the SearchPage `:79` exclusion** +
B's `ApiError.category` + A's "only toast silent-load failures when server ok."

---

## Best frontend design = take X from plan Y

| Concern | Take from | Why |
|---|---|---|
| Status fetch helper | **A — `getStatus()`** | Keeps `authHeaders` private; explicit `needs-auth` vs `unreachable`; own 8s timeout; never clears key |
| Observed store / re-render | **D — `useSyncExternalStore` singleton** | Only design that avoids provider-wide re-render storms; transport-agnostic |
| Observed downgrade/self-heal rules | **A** (`applyObserved`, `llm-ok` self-heal) + **B/D** ("neterr/stall AND no-byte-8s"→unreachable) | Bounds false positives; fastest honest self-heal |
| Auth invariant safety | **A or D** (non-throwing helper, `api()` 401-throw intact) | Provably cannot route a poll 401 into logout |
| Provider mount | **B — above the auth gate** + **carve-out so it does NOT wrap `/share/:token`** | Pre-auth reachability on KeyEntry; avoids polling on public share devices |
| Status route auth posture | **B/D soft-auth** (public skeleton `{ok,brain}`) | Makes 401 impossible on the poll; safe even if accidentally called pre-auth/on share |
| Gating inventory | **C wholesale** (incl. a0 search fix, video-vision note, entity deferred-rebuild note, Labs Extract button, Map-gate removal, OwnerChat NOT gated) | Only inventory that survives line-by-line spot-checking |
| Public-share pre-flight | **C — server-driven `llm_ready` landing flag** | Only honest option for the un-gateable public route |
| Toast | **C — bus-fed + `explainError` + SearchPage `:79` exclusion** + **B's `ApiError.category`** | Avoids keystroke toast storm; shared classification |
| Indicator UX | **D/B three-axis** (browser-offline / server-unreachable / subsystem-degraded) + **A's `needs-auth` 4th state** | Distinguishes the cases the current `navigator.onLine`-only banner conflates |

---

## Top remaining frontend MUST-FIXES for the hybrid

1. **[HIGH] Adopt C's `search.py:80-92` try/except (a0) — it is NOT optional.**
   Every plan's "hybrid/semantic degrades to keyword" gate is a lie without it;
   A's design actively assumes a fallback that does not exist and would hang/500
   on every keystroke during warmup. Pair with C's "force keyword while embeddings
   not ready."
2. **[HIGH] Use a non-throwing poll helper (A's `getStatus()`) so a poll 401 can
   never reach `clearAccessKey`.** Reject Plan C's ambiguous "bubble 401 to the
   App handler." Keep `api()`'s 401-throw intact so `App.tsx:106` stays the sole
   logout site.
3. **[MED] Mount the StatusProvider above the auth gate (B) BUT carve out
   `/share/:token`** so the poller never runs on a public share recipient's
   device. Use a soft-auth route so even a stray call returns a harmless skeleton.
4. **[MED] Put the observed/health store in a `useSyncExternalStore` singleton
   (D), not in `AuthCtx` (A).** Threading caps through AuthCtx re-renders the whole
   authed tree every poll + every observed tick.
5. **[MED] Drop B's wrong gates** (OwnerChatPage llm-gate — it's E2EE human chat;
   Map geocoder gate — labels are pre-resolved). Take C's corrected rows.
6. **[MED] Require failed-poll OR sustained observed signal before the dot goes
   red** — a single transient 5xx must not flip it. Use B/D's "neterr/stall AND no
   server byte within ~8s" conjunction; A's "5xx + failed-poll → unreachable" is
   the right shape.
7. **[MED] Keep C's SearchPage `:79` toast exclusion** to avoid a keystroke-rate
   toast storm on cold boot.
8. **[LOW] Keep `authHeaders` private** (reject D's "export authHeaders");
   export A's single purpose-built helper instead.
9. **[LOW] Make A's observed instrumentation explicit** about *adding* try/catch
   around `streamChat`'s `:735` fetch and `streamSSE`'s `:806` fetch (it's outside
   the read-loop try) — B/C/D already say this; A's wording is loose.
10. **[LOW] Adopt A's 4-state server health** (`ok`/`unreachable`/`needs-auth`/
    `unknown`) so a rotated key shows amber "Re-authenticate," not red
    "unreachable."

---

## Residual risks the hybrid still carries (all honest, accepted)

- **LLM validity is observed, not proactive** — a revoked key shows green until the
  first real call fails (then `degraded`). Cost-driven; unavoidable without token
  burn. Affects the public-share landing flag too.
- **Silent subsystem death with no traffic** lags up to one poll interval (20s).
- **Per-process readiness flags** — single uvicorn worker today; latent if anyone
  adds `--workers`. All four flag it; keep the LOUD comment.
- **Gating drift** — the inventory is a hand-maintained artifact; the toast layer
  is the backstop for any missed surface. Keep the exhaustiveness test (C).
