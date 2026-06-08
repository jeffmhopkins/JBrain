# Red-Team 1 — Critique of Plan C (Capability-Manifest-Driven UI Gating)

**Reviewer role:** adversarial. **Target:** `12-plan-C-capability-gating.md`.
**Verdict up front:** Plan C has the best *gating UX rigor* of the four and a
genuinely correct, cheap backend manifest design. But its central selling point —
the "EXHAUSTIVE feature→capability inventory" — contains **several material
inventory errors that would ship broken or no-op gates**, and it under-delivers
the "real-time *server* health" half of the stated goal by its own admission.
The inventory is presented as the rigorous heart of the plan; under audit it is
the weakest part.

---

## 1. Correctness bugs (claims/code/inventory vs. reality)

### 1a. HIGH — Semantic/hybrid search does NOT gracefully fall back to FTS on the server
Plan C repeatedly relies on the server degrading semantic→keyword automatically:
- §c Search table, hybrid row: *"Allowed even when embeddings `warming` (server already falls back to FTS)."*
- §c NotePage Rebuild row: *"degrades to keyword if embeddings `warming` (server already handles)."*

**This is false for the note/attachment semantic path.** In
`server/app/routers/search.py:80-92`, the `do_semantic` block calls
`embeddings.semantic_search(...)` and `embeddings.semantic_search_attachments(...)`
**with no `try/except`**. Every other branch in that function (keyword notes
`:37-48`, keyword attachments `:50-64`, keyword entities `:69-78`, *semantic
entities* `:94-103`) is individually wrapped — but the two note/attachment
semantic calls are not. If embeddings are `warming`/`unavailable`,
`_get_model()` (`services/embeddings.py:20-30`) **blocks on the model load** (or
raises), so a `hybrid` **or** `semantic` query will hang on first warmup or 500 —
it does **not** silently return FTS results. Plan C's whole "let hybrid through,
the server handles it" stance is built on a fallback that doesn't exist. This is
the most consequential inventory error because it inverts the safe default:
Plan C would *leave hybrid enabled* on the assumption it degrades, when in fact
it's the path most likely to fail.

(Plan A's instinct here is better: it forces keyword when embeddings aren't ready.)

### 1b. HIGH — `GuidedChat` / `ResearchChat` are on the PUBLIC share route, outside the Capabilities provider
§c NotePage table (line 297) lists *"Talk / Guided / Research chat embeds
(`TalkPanel.tsx`, `GuidedChat.tsx`, `ResearchChat.tsx`)"* under **NotePage** and
prescribes gating them with `CapabilityButton cap="llm"`.

Reality (`web/src/pages/SharePage.tsx:9-10,67,73`): **`GuidedChat` and
`ResearchChat` are rendered by `SharePage`**, which is the standalone public
`/share/:token` route — `App.tsx:124`, mounted *outside* the authed branch, with
**no `Shell` and no auth context**. Plan C mounts `CapabilitiesProvider` "inside
the authed branch, around `<Shell>`" (§B2, line 193). Therefore `useCapability`
is **unavailable** on SharePage; calling it there returns the null context /
throws. The plan both (a) misattributes these components to NotePage and (b)
prescribes a gating primitive that structurally cannot run where they live. Only
`TalkPanel` is actually a NotePage embed (`NotePage.tsx:221`, KB notes only).

This also exposes a blind spot: the public share surfaces are LLM-dependent and
the manifest is key-gated, so Plan C has **no story for pre-flighting the public
share experience** at all.

### 1c. MEDIUM — "Search submit" / semantic toggle anchors are wrong; there is no submit button
§c Search rows anchor on `SearchPage.tsx:36,77`. `SearchPage.tsx:36` is the
`const MODES` array, not state, and `:77` is inside the *search-as-you-type*
effect. Critically, **SearchPage has no submit button** — it debounces and
re-queries on every keystroke and on mode change (`SearchPage.tsx:71-82`). Plan A
and B both say "Search submit," and Plan C says "force-select keyword / disable
the semantic toggle," which is at least the right shape — but the plan never
acknowledges the live-as-you-type model, which means a `warming` embeddings state
will fire a failing semantic request on **every keystroke** until the toggle is
gated. The transient errors are currently swallowed (`SearchPage.tsx:79`
`catch { /* ignore */ }`), so today it's invisible; with Plan C's
`explainError` toast wiring (§e) this could become a keystroke-rate toast storm
unless special-cased.

### 1d. MEDIUM — MapPage does no client-side geocoding; the "coordinates-only" gate is mostly a no-op
§c Advanced Map row (line 314) and §B1 copy promise an address-label gate driven
by `geocoder`. But `web/src/pages/MapPage.tsx` never calls a geocoder; labels
come pre-resolved from stored note data (`location_label`, `MapPage.tsx:212`;
place/head tooltips `:356-370,470`). Geocoding is entirely server-side
(`services/geocode.py`, used by capture/scheduler). So a client-side
"geocoder unconfigured → coordinates-only" note has nearly nothing to attach to;
the geocoder capability barely surfaces in the PWA. Including it in the manifest
is fine (cheap), but the inventory oversells a user-facing gate that has no real
trigger point.

### 1e. MEDIUM — Entity rebuild is under-gated
§c Advanced Entities row (line 313): *"rebuild action needs nothing extra."*
Entity rebuild embeds entities (uses **embeddings**, `embeddings.store_entity_vector`)
and its KB-article quality depends on **LLM**. Plans A and B both correctly tag
entity rebuild as `llm` (+embeddings). Plan C says "nothing extra" and defers to
the existing `entities/status` poll — which reports *rebuild progress*, not
*capability readiness*. A keyless box still shows the rebuild button as fully
enabled; it will produce a degraded/partial result. This is exactly the
"feature that can't fully run isn't warned about before use" failure the goal
targets.

### 1f. LOW — `LabImportPanel` has no "AI import" button to gate
§c Advanced Labs row (line 317) prescribes gating an *"AI import"* button in
`LabImportPanel.tsx`. That component is review/approve/revoke/reanalyze of
*already-extracted* labs; the LLM extraction actually happens at upload time via
`extractLabs(...)` in `Chat.tsx:558-562` (which Plan C *also* lists separately and
correctly). So the Labs-card gate points at a control that doesn't exist; the
real gate is the Medical-capture lab-extraction path it already enumerated.

### 1g. LOW — Citation drift in §0/§a "verified ground truth"
- `config.py:70-81` for `has_anthropic/has_xai/has_llm`: the properties sit
  *after* the audio/video/VAPID config blocks, not at 70-81. `geocoder_url`
  (`config.py:39`) is approximately right.
- `main.py:244` for the router loop: the `for r in (...)` registration is at
  ~`:248`; the embeddings warmup `_get_model` call is ~`:180` (plan says 202),
  with `create_task` at ~`:202`. Off by a handful of lines throughout.
- `ModelPicker.tsx:30-66`/`:47-66`: the `/verify` re-fetch is on `:37` inside
  `load()`; the missing-key warning is computed `:48-52`, rendered `:61-66`. The
  ranges are loose but the substance (it re-fetches `/verify` and already warns)
  is correct.

None of 1g is fatal, but a plan that brands itself on an *exhaustive, verified*
inventory should hit anchors precisely; the drift undermines confidence in the
table that is supposed to be the deliverable.

### Correct claims worth crediting
- `/api/capabilities` genuinely does not exist yet (verified).
- AdvancedHome card line anchors (`:14`–`:39`) are **all exactly correct** —
  the one part of the inventory that's pinpoint accurate.
- `hasLlm` is consumed in exactly one place (`Attachments.tsx:38,197,290`) — correct.
- Composer gates on `!online` not `has_llm` (`Chat.tsx:505,922,947`) — correct;
  Research/Full Brain do not gate on LLM today — correct.
- Backend readiness primitives (`push.public_key()` `push.py:67`,
  `geocode.enabled()` `geocode.py:38`, `has_*` props) all exist as assumed.
- Offline-tolerant auth (`App.tsx:106`) and the "don't log out on non-401"
  contract are read correctly, and §B2/§f respect it.

---

## 2. Goal gaps — real-time *server* health is under-served (admitted)

The owner's goal is **two** things: (1) real-time server *and* API health, and
(2) pre-flight "this won't work" warnings. Plan C is explicitly lopsided:
"the server/API health indicator is real but deliberately lightweight" (§
philosophy), and §d is literally titled "(lighter, but real & real-time)."

- **"Real-time" is 20s polling**, paused when hidden (§A3, §B2). The plan's own
  Risk 3 concedes "up to ~20s before the UI notices." That is *near*-real-time,
  the same coarseness as Plan A/B, but Plan C **doesn't even adopt Plan A's
  adaptive cadence** (5s while warming). So warming→ready — the *one* transient
  the plan says "is the only transient that matters" (§A3) — can lag up to 20s,
  during which the UI either gates a now-ready feature or (worse, per 1a) lets a
  not-ready one through. Plan A's 5s-while-warming is strictly better for the
  exact case Plan C cares about.
- **No observed-traffic signal.** Plan D's key insight — that `api()`/`streamChat`
  already see every 5xx/stall and can flag a dead server *between* polls — is
  absent. Plan C's only server-reachability signal is "did the 20s poll succeed."
  A server that dies 1s after a poll shows green for ~19s. For the "server
  unreachable" half of the goal this is the weakest of the four plans.
- **Degraded ≠ reachable granularity is thin.** `ServerHealth` is
  `ok|degraded|unreachable|unknown` derived solely from the poll (§d). It cannot
  distinguish "server up, one subsystem down" caused by a *runtime* failure
  (e.g. a revoked LLM key that fails at call time) — that only surfaces via the
  §e error fallback, never the dot. Plan B's `last_error` and Plan D's observed
  downgrade both do better here.

Net: Plan C delivers the *pre-flight gating* half excellently and the *real-time
server health* half adequately-but-least-ambitiously. Given the goal weights both,
this is a real gap, not just a stylistic choice.

---

## 3. Risk & robustness

### 3a. HIGH — Drift / maintenance of exhaustive manual gating (the plan's own #1 risk, and it's worse than stated)
Plan C is candid (Risk 1) that nothing forces a new feature to add a gate. But
its mitigations are weaker than advertised:
- The `CAP_COPY` exhaustiveness test only catches a **new capability** with no
  copy. It does **nothing** for the far more common case: a new *consumer* of an
  *existing* capability shipped without a gate. The plan admits this, then leans
  on the §e error fallback as the backstop — which means **the frontend-centric
  thesis quietly depends on the error-surfacing layer it deprioritizes.**
- "Checklist comments point here" is the weakest possible enforcement — a comment
  in `AdvancedHome.tsx`/`capabilities.ts` is invisible to anyone editing
  `Chat.tsx` or a new page.
- The inventory **is already wrong on day one** (§1a/1b/1d/1e/1f above). If the
  authoring team can't get the *initial* exhaustive pass right under review, the
  steady-state drift will be worse. This is the empirical proof of the risk: the
  artifact that's supposed to be the rigorous heart shipped with multiple
  no-op/misplaced/broken rows.

**Severity HIGH.** The architecture amplifies a maintenance burden that the
codebase's own philosophy (fail-closed + lazy load) was designed to avoid. Every
other plan shares some manual-gating breadth, but Plan C makes the *exhaustive
table* a load-bearing deliverable, so it owns the most drift surface.

### 3b. MEDIUM — Race between gate and reality (admitted, Risk 3)
20s poll + no observed-traffic feed ⇒ widest race window of the four. Bounded by
immediate refresh on focus/online, but a user mid-session who starts a
just-failed feature gets no signal until §e catches the error. Acceptable only
*because* §e exists — again coupling the thesis to the fallback.

### 3c. MEDIUM — `warming` flicker and the search-as-you-type interaction
On cold boot, embeddings/audio are `warming` for seconds. Combined with 1c
(live search) and 1a (no server fallback), a user who opens Search right after a
deploy and types in `hybrid` (the default mode, `SearchPage.tsx:44`) fires
failing semantic requests per keystroke. Plan C must force keyword while
embeddings aren't `ready` *and* fix/relocate the server-side fallback, or this is
a visible cold-start regression.

### 3d. LOW — Offline-tolerant auth: handled correctly
§B2/§f keep the manifest sticky and never clear the key on non-401; the poll
bubbles 401 to the existing `App.tsx:106` path. No regression. Good.

### 3e. LOW — Cost / security: fine
Key-presence-only LLM readiness (no token burn), key-gated endpoint (no pre-auth
leak beyond `/auth/info`), cheap flag reads. All constraints in §8 are met as
claimed. The one nuance — present-but-invalid key shows ready — is correctly
called out (Risk 2) and is unavoidable without burning tokens.

### 3f. LOW — Single-process assumption
Like all four plans, readiness flags are per-process/in-memory. JBrain is
single-process today (verified: warmups are `asyncio.create_task` in one
lifespan, `main.py`), so latent only. Plan C doesn't mention it; Plan A does.

---

## 4. Inventory completeness — entry points it missed or misplaced

Beyond the misattributions in §1, the "exhaustive" inventory misses:

1. **Public share LLM surfaces** (`SharePage` → `GuidedChat`, `ResearchChat`,
   `SharePage.tsx:67,73`). Not just misfiled (1b) — there is **no plan** to
   pre-flight the public share chat, which is wholly LLM-dependent and lives
   outside the manifest's reach. The owner who creates a share link can't be
   warned the shared experience won't work if the key is absent.
2. **Owner-assisted encrypted chat page** (`OwnerChatPage`, route
   `/shares/chat/:linkId`, `App.tsx:138`) streams LLM replies
   (`chatOwnerStreamPath`, `OwnerChatPage.tsx:53`). Plan C's Shares row says
   "gate that control only," but this is a whole route, not a control, and isn't
   enumerated as such.
3. **Research external-lookup approval flow** in Chat
   (`approveProposal`/`skipProposal`, `Chat.tsx:713-724`) — these `send()` a
   follow-up turn, i.e. LLM-dependent, reachable only inside Research/Full. If
   the parent mode gate is correct they're covered transitively, but the plan
   never says so.
4. **Video frame vision summary** during transcription
   (`audio_transcription.py:255`, gated server-side on `llm.has_credentials()`).
   A user transcribes a video expecting the visual summary; with no key they get
   transcript-only silently. Plan C gates "Analyze with AI (image)" for vision
   but never the video-vision sub-feature.
5. **PDF/medical lab extraction** is listed for the Medical *capture* path
   (`Chat.tsx:558`) but the same `reanalyzeLabs` path inside `LabImportPanel`
   (the actual Labs UI) is mis-described (1f) rather than enumerated.
6. **Note "Talk" on KB pages** vs the share Guided/Research split — the plan
   conflates three distinct components into one row, obscuring that two of them
   live on an un-gateable route.

To be fair, the plan *did* catch the high-value ones (Chat modes, Attachments,
note AI analysis, Rebuild family) and pinned AdvancedHome anchors precisely.

---

## 5. What Plan C does BETTER than A/B/D (keep these)

1. **The richest, most honest degradation vocabulary applied to UX.** The
   `warming` vs `unavailable` vs `unconfigured` distinction, mapped to a single
   `CAP_COPY` table (§B1) and three shared primitives (`RequiresCapability`,
   `CapabilityButton`, `CapabilityNote`, §B3), is the cleanest "explain *why* and
   *what to do*" design of the four. Plan A/B/D have gates; only C makes the
   *copy* a first-class, single-sourced, tested artifact. This is genuinely the
   best pre-flight UX.
2. **One request serves both health and capabilities** (§A3) — the manifest's
   success *is* the reachability signal. Minimal, elegant, cross-origin-safe.
3. **`disable + explain` over hide, and never gating local/offline-safe actions**
   (Entry capture, keyword search, attach, transcribe-when-ready). The principle
   is articulated more crisply than the others.
4. **a11y is in the plan** (`aria-disabled`, `title`, spinner affordance for
   `warming`) — none of the other plans mention accessibility of disabled
   controls.
5. **The exhaustiveness *test* idea** (snapshot asserting every reachable
   `CapState` has copy) is a good drift guard even if it only covers part of the
   risk.
6. **Pinpoint AdvancedHome anchors** — proof the author actually walked that file.

---

## 6. What Plan C should STEAL from A/B/D

- **From Plan A:** the **adaptive poll cadence** (5s while warming, 20s steady).
  It directly fixes Plan C's biggest real-time weakness for the one transient it
  cares about, at negligible cost.
- **From Plan D:** the **observed-traffic health feed** — instrument the central
  `api()` wrapper (and `streamChat`) so a 5xx/network/stall between polls flips
  the dot to `unreachable`/`degraded` immediately. This is the cheapest possible
  upgrade to the "real-time *server* health" half and needs no SSE.
  Also steal D's correct placement of the share surfaces (D's gating table at
  least scopes "research shares" as llm-gated, acknowledging they exist).
- **From Plan B:** a small **`last_error`/`detail` field per subsystem** so the
  dot/panel can say *why* something is degraded (e.g. "embedding model failed to
  load"), and B's **soft-auth public skeleton** so a `server-unreachable` vs
  `browser-offline` distinction works pre-login on KeyEntry too (Plan C's
  manifest is key-gated, so it has no reachability signal before auth).
- **From Plan A's gating principle (and to fix 1a):** *force keyword when
  embeddings aren't `ready`* rather than trusting a server fallback that doesn't
  exist — and additionally wrap the note/attachment semantic calls in
  `search.py:80-92` in try/except so the backend actually degrades.

---

## 7. Verdict

Plan C is the **best pre-flight gating UX** and a clean, cheap, constraint-
respecting backend manifest — but it is **mis-sold as having an exhaustive,
verified inventory when that inventory ships with multiple broken, no-op, or
misplaced rows**, and it under-serves the *real-time server health* half of the
mandate it was given. The plan's own structure makes the manual inventory the
load-bearing deliverable, so its errors and its drift risk hit hardest exactly
where it stakes its claim. It is salvageable and arguably the right *spine* for
the gating UX, but it needs the inventory re-audited and a real-time/observed
signal grafted on from D (and cadence from A).

### Top 5 must-fix (ranked)
1. **Fix the false "server falls back to FTS" assumption (1a).** Wrap the
   semantic note/attachment calls in `search.py:80-92` in try/except *and* gate
   the UI to force keyword while embeddings aren't `ready`. This is a real
   server bug the inventory papers over.
2. **Re-home the public share surfaces (1b/4.1).** `GuidedChat`/`ResearchChat`
   live on the un-authed `/share/:token` route, outside the Capabilities
   provider; the prescribed `useCapability` gate cannot run there. Add a real
   plan for pre-flighting public shares (or scope them out explicitly).
3. **Graft on a real-time server signal:** adaptive cadence (5s-while-warming,
   from A) + observed-traffic flips via the `api()` wrapper (from D). Closes the
   admitted ≤20s blind spot and the "server died right after a poll" hole.
4. **Re-audit the rest of the inventory** for the no-op/misplaced rows: Map
   geocoder (1d), entity rebuild llm+embeddings (1e), Labs "AI import" (1f),
   video-vision sub-feature (4.4). The "exhaustive" table must actually be
   correct to justify the plan's thesis.
5. **Strengthen drift enforcement beyond comments:** a lint/test that fails when
   an LLM/embeddings/audio API call is reachable from a component with no
   `useCapability` in scope is hard, but at minimum require the inventory doc to
   be CI-checked against the route list, and treat §e (error fallback) as a
   first-class equal partner, not a deprioritized safety net — the thesis already
   silently depends on it.
