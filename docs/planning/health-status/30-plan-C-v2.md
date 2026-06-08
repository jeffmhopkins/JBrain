# Plan C v2 — Capability-Manifest-Driven Exhaustive Gating (corrected & real-time)

**Author:** Architect C (v2) · **Supersedes:** `12-plan-C-capability-gating.md`
**Philosophy (unchanged):** Never let a user start something that can't succeed.
A single typed capabilities manifest, supplied honestly by the backend, is
consumed through a `Capabilities` context + `useCapability()` hook and a small
set of shared gating primitives. Every feature entry point is mapped to the
capability/subsystem it needs and given a consistent disabled-with-reason state.
Exhaustive, consistent pre-flight gating is the heart of the work.

**What's new in v2:** the inventory is *re-verified against the code line by
line* (v1's was the deliverable and it shipped with bugs), a real server bug is
fixed so the gates don't rest on a fallback that doesn't exist, the public share
surfaces get a real pre-flight story, and the "real-time server health" half is
genuinely upgraded with Plan A's adaptive cadence and Plan D's observed-traffic
feed — instead of being "deliberately lightweight."

---

## 0. Changes from v1 / red-team responses

Every must-fix from `20-redteam1-C.md`, plus corrections I found while
re-auditing. Each is verified against the cited code.

### HIGH — 1a: Semantic/hybrid search does NOT fall back to FTS on the server
**v1 was wrong.** `search.py:80-92` calls `embeddings.semantic_search(...)` and
`embeddings.semantic_search_attachments(...)` with **no try/except** — verified:
the keyword-notes (`:37-48`), keyword-attachments (`:50-64`), keyword-entities
(`:69-78`) **and** *semantic-entities* (`:94-103`) branches are each wrapped, but
the two note/attachment semantic calls at `:80-92` are bare. Both call
`embed(query)` → `embed_many` → `_get_model()` (`embeddings.py:20-30`), which
blocks on the model load under a lock while `warming`, or raises `ImportError`
when `unavailable`. So a `hybrid` **or** `semantic` query hangs on first warmup
or 500s — it does **not** silently return FTS results.

**v2 fixes BOTH halves:**
- **(a) Backend:** wrap the two semantic calls in try/except so the function
  degrades to whatever keyword/entity results it already gathered (§a0). This is
  a genuine pre-existing bug; fixing it is the right safe default regardless of
  the UI work.
- **(b) Gating:** invert v1's stance. v1 left `hybrid` enabled "because the
  server falls back"; v2 **forces keyword** whenever `embeddings` isn't `ready`
  (steal Plan A's principle), and disables the `semantic` toggle. `hybrid` stays
  selectable but runs keyword-only with an honest note while embeddings warm
  (now true, because of (a)). Corrected rows in §c Search.

### HIGH — 1b/4.1/4.6: Public share LLM surfaces live outside the provider
**v1 was wrong twice.** It filed `GuidedChat`/`ResearchChat` under **NotePage**
and prescribed `CapabilityButton cap="llm"`. Verified:
- `GuidedChat`/`ResearchChat` are rendered **only by `SharePage`**
  (`SharePage.tsx:9-10,67,73`) on the public `/share/:token` route
  (`App.tsx:124`), mounted **outside** the authed branch — no `Shell`, no auth
  context, no Capabilities provider. They take props (`token`, `brainName`,
  `intro`…), do **not** import `useAuth`/`App` (verified: `GuidedChat.tsx:1-3`,
  `ResearchChat.tsx:3`), so `useCapability` structurally cannot run there.
- Only **`TalkPanel`** is a real NotePage embed (`NotePage.tsx:221`, KB notes
  only). `AiAnalysisPanel` (`:219`) and `RebuildPanel` (`:372`) are the other
  NotePage panels.

**v2 design — server-driven pre-flight for public shares (§a2 + §c.Share):**
the share landing already half-does this. `_resolve_guided`/`_resolve_research`
(`share.py:187-194,287-294`) **already 404** when `not llm_ready()`
(`share.py:197-199`, which returns `llm.has_credentials()`) — but only on
`start`/`turn`, and as a generic "This link isn't available" 404 that reads like
a *revoked* link to the recipient. v2 adds an honest, lightweight, server-driven
flag to the **landing** payload (`_guided_landing` `:163`, `_research_landing`
`:261`, both reached via `share_read` `:107-117`): include `"llm_ready":
llm_ready()`. `SharePage` (an unauthed `publicApi` fetch, `api.ts:131,144`) reads
it and renders a clear "This assistant is temporarily unavailable — please check
back later" landing instead of letting the recipient start a chat that 404s.
No manifest, no auth, no token leak — one boolean already computed server-side.

### HIGH — Real-time server health under-served
v1 admitted 20s polling and **didn't even adopt Plan A's adaptive cadence**, and
had **no observed-traffic signal**. v2 grafts both (§d):
- **Plan A's adaptive cadence:** 5s while any subsystem is `warming`, 20s steady,
  paused when hidden, immediate on focus/pageshow/online. Verified the existing
  `ReviewBell` already uses exactly this resume pattern
  (`Shell.tsx:38-39,49-51` — incl. `pageshow`, which mobile/PWA needs).
- **Plan D's observed-traffic feed:** instrument the central `api()` wrapper
  (`api.ts:40-57`, the single chokepoint behind `get/post/put/del`) and
  `streamChat`/`openChatStream`/`streamSSE` so a 5xx/network/stall between polls
  flips the dot immediately and triggers a snapshot re-fetch. No SSE, no new
  transport. (Plan D's red-team correctly notes the SSE stream is overkill for a
  single-user app; v2 takes only the observed-feed idea, which needs no stream.)
- Adds a per-subsystem `detail`/`last_error` (steal from Plan B) so the dot/panel
  can say *why* it's degraded.

### Re-audited inventory corrections
- **1d — Map geocoder gate is a near-no-op.** Verified `MapPage.tsx` never
  geocodes client-side; labels come pre-resolved (`location_label`,
  `MapPage.tsx:212`). Geocoding is server-side only (`geocode.py`). v2 **removes**
  the Map "coordinates-only" UI gate. `geocoder` stays in the manifest (cheap,
  diagnostic, shown in the System panel) but is **not** sold as a user-facing
  PWA gate.
- **1e — Entity rebuild needs llm + embeddings.** Verified `entity_index.rebuild`
  uses `embeddings.embed_many`/`store_entity_vector` (`:430-432`) and
  `llm.complete`/`has_credentials()` (`:802,819`). v1's "needs nothing extra" was
  wrong. **Correction with a nuance v1 *and* the red-team missed:** there is **no
  standalone "Rebuild all" button** in `EntitiesPage` — the rebuild is *deferred
  and coalesced*, triggered by **merge/split/alias** edits
  (`entities.py:40-66`), watched via `getEntityRebuildStatus`
  (`EntitiesPage.tsx:96`). So the gate is not a button to disable; it's a
  **pre-edit note** on the identity controls explaining that with no LLM/embeddings
  the fold will produce a degraded (no KB article / no vector) result, plus
  surfacing `last_error` from the existing status poll. Corrected row in §c.
- **1f — Labs "AI import" button doesn't exist.** Verified `LabImportPanel.tsx`
  has **no** "AI import" control; its LLM-backed action is the **"Extract lab
  values" / "Re-analyze"** button (`LabImportPanel.tsx:58`, `reanalyzeLabs` →
  `medical.py:139` → `lab_vision.py:86` `has_credentials()`). v2 gates **that
  button**, not a phantom one.
- **4.4 — Video-frame vision sub-feature was missed.** Verified
  `audio_transcription.py:255`: a *video* transcribe also runs a vision summary
  **gated server-side on `llm.has_credentials()`**. With no key the user gets
  transcript-only silently. v2 adds a row: a note on the video transcribe path
  that the visual summary needs `llm`.
- **4.2 correction (red-team itself was wrong).** The red-team claimed
  `OwnerChatPage` "streams LLM replies." Verified it does **not**: `kind="chat"`
  is an **E2EE human-to-human** channel — message bodies are opaque ciphertext to
  the server (`share.py:527-548`); `chatOwnerStreamPath` streams the *peer's*
  messages, not LLM output (`OwnerChatPage.tsx:51-57,98`). So `OwnerChatPage`
  needs **no LLM gate**, and v1's "Shares: owner-assisted share chat needs llm"
  note was *also* wrong. The only LLM-dependent share kinds are `guided` and
  `research`. Corrected in §c.

### Drifted line citations fixed
- `config.py` `has_anthropic/has_xai/has_llm` are at **`:71-81`** (v1 said
  `:70-81`); `geocoder_url` at **`:39`**. ✓
- Router registration loop is at **`main.py:244`** (v1 said 244; red-team's "~248"
  was the off-one). Embeddings warmup `_get_model` at **`:180`**, `create_task`
  at **`:202`**; audio `_get_model` at **`:211`**, `create_task` at **`:215`**.
- `ModelPicker` re-fetches `/verify` at **`:37`**, computes missing-key set at
  **`:48-52`**, renders the warning at **`:61-66`**.
- Attachments: help copy **`:197`**, Transcribe button **`:285`**, Analyze button
  **`:290`** (all confirmed exact).
- AdvancedHome card anchors **`:14`–`:39`** re-confirmed exact (the one part of
  v1 that was pinpoint).

### Kept (red-team praised these — do not regress)
warming/unavailable/unconfigured/unknown vocabulary; single-sourced `CAP_COPY`;
the three shared primitives (`RequiresCapability`, `CapabilityButton`,
`CapabilityNote`); a11y on disabled controls; **disable-and-explain** over hide;
**one request serves both** health + capabilities; accurate AdvancedHome anchors;
the **copy-exhaustiveness test** (extended in v2, §Testing).

---

## A. Backend (minimal, honest, lean)

### a0. Fix the real bug: degrade semantic search to keyword (`search.py:80-92`)

Wrap the two bare semantic calls so a warming/unavailable embedding model can
never hang or 500 the whole search — it falls back to the keyword/entity hits
already collected:

```python
if do_semantic and not entity_only:
    try:
        for i, r in enumerate(embeddings.semantic_search(conn, q, limit)):
            bump(f"note:{r['id']}", { ... }, i)
        for i, r in enumerate(embeddings.semantic_search_attachments(conn, q, limit)):
            bump(f"att:{r['attachment_id']}", { ... }, i)
    except Exception:
        pass   # embeddings warming/unavailable → return keyword (+entity) results
```

This mirrors every other branch in the function and makes the gating row
("hybrid runs keyword-only while warming") *true*. It is independently correct.

### a1. New endpoint: `GET /api/capabilities` (key-gated)

One consolidated, **cheap** manifest. Key-gated (same posture as `/verify`); no
pre-auth leak. Reads cached process flags + cheap config props only — no model
load, no DB scan, no token burn.

```jsonc
// GET /api/capabilities  (CurrentUser)
{
  "server": { "version": "1.42.0", "app_tz": "America/New_York",
              "owner_set": true, "uptime_seconds": 3600,
              "ts": "2026-06-08T15:04:05Z" },
  "capabilities": {
    "llm":        { "ready": true,  "state": "ready",
                    "providers": { "anthropic": true, "xai": false },
                    "detail": null },
    "embeddings": { "ready": false, "state": "warming", "detail": null },
    "audio":      { "ready": true,  "state": "ready",   "detail": null },
    "geocoder":   { "ready": true,  "state": "ready",   "detail": null },
    "push":       { "ready": true,  "state": "ready",   "detail": null }
  }
}
```

`state` ∈ `"ready" | "warming" | "unavailable" | "unconfigured" | "unknown"`.
`ready` is shorthand (`state === "ready"`). `unknown` covers the pre-first-load
window (steal from Plan A/B — v1 lacked it). `detail` carries a truncated
`last_error` for `unavailable` (steal from Plan B), post-auth only.

| Capability | derivation | cost |
|---|---|---|
| `llm` | `ready` iff `settings.has_llm` (`config.py:80-81`), else `unconfigured`; `providers={has_anthropic,has_xai}` (`config.py:71-78`) | key **presence only**, never a live call |
| `embeddings` | `embeddings.readiness()` (§a3) | flag read |
| `audio` | `audio_transcription.readiness()` (§a3) | flag read |
| `geocoder` | `ready` iff `geocode.enabled()` (`geocode.py:38`), else `unconfigured` | config |
| `push` | `ready` iff `push.public_key()` (`push.py:67`) non-empty | config/DB meta |

New `server/app/routers/capabilities.py` assembles it; register in the
`main.py:244` router loop.

### a2. Server-driven public-share readiness (no manifest, no auth)

In `_guided_landing` (`share.py:163`) and `_research_landing` (`:261`), add
`"llm_ready": llm_ready()` to the returned dict (the helper already exists,
`share.py:197-199`). This rides the existing public `GET /api/share/{token}`
landing (`share_read` `:107-117`) — one boolean, already computed, no token leak
(it reveals only that *the owner's* assistant is up, which the recipient is about
to find out anyway). This is the pre-flight signal for the un-gateable public
route. (Defense-in-depth: the `start`/`turn` 404 stays.)

### a3. Readiness probes in the lazy services (the only real new backend state)

Non-blocking, O(1), no model touch.

**`services/embeddings.py`** — add `_import_ok = True` and:
```python
def readiness() -> str:        # 'unknown' | 'warming' | 'ready' | 'unavailable'
    if not _import_ok: return "unavailable"
    if _model is not None: return "ready"
    return "warming" if _warm_started else "unknown"
```
Set `_warm_started = True` at the top of `_get_model()` (before the import), and
`_import_ok = False` in an `except ImportError` around the `from fastembed import
TextEmbedding` (`embeddings.py:25`). The boot warmup
(`main.py:180` `to_thread(_get_model)`) flips it to `ready` seconds after boot.

**`services/audio_transcription.py`** — same shape, **but key off the model
config** so a Settings-driven model swap isn't reported as stale `ready` (this is
the bug Plan A's red-team caught at `audio_transcription.py:98`,
`_model is None or _model_key != want`):
```python
def readiness() -> str:
    if _import_failed: return "unavailable"     # set in the existing ImportError branch (:103-107)
    want = (audio_model(), audio_compute_type())
    if _model is not None and _model_key == want: return "ready"
    return "warming" if _warm_started else "unknown"
```
Audio's `_get_model` already raises `TranscriptionUnavailable` on missing dep
(`:104-107`); flip `_import_failed=True` there. `_warm_audio` (`main.py:211`)
flips it `ready` on a healthy box.

Both probes are pure reads — safe at any cadence (constraint: cheap & frequent).

### a4. Freshness ("real-time" feel)

The PWA polls `/api/capabilities` on the shared adaptive interval (§d). The
manifest's success **is** the "server reachable + authed" signal — **one request
serves both** health and capabilities (kept from v1). The observed-traffic feed
(§d) closes the between-polls gap so the dot is genuinely real-time, not 20s-stale.

---

## B. Frontend context + hooks + shared primitives (kept, with `unknown`)

### B1. Types & single-sourced copy (`web/src/capabilities.ts` — new)

```ts
export type CapState = "ready" | "warming" | "unavailable" | "unconfigured" | "unknown";
export type CapId = "llm" | "embeddings" | "audio" | "geocoder" | "push";

export interface Capability {
  ready: boolean; state: CapState; detail: string | null;
  providers?: { anthropic: boolean; xai: boolean };   // llm only
}
export interface CapabilitiesManifest {
  server: { version: string; app_tz: string; owner_set: boolean;
            uptime_seconds: number; ts: string };
  capabilities: Record<CapId, Capability>;
}
export type ServerHealth = "ok" | "degraded" | "unreachable" | "unknown";

// ONE source of truth so every gated surface explains itself identically.
export const CAP_COPY: Record<CapId, Partial<Record<CapState, string>>> = {
  llm: {
    unconfigured: "AI features need an API key. Set LLM_API_KEY (Claude) or XAI_API_KEY (Grok) on the server.",
  },
  embeddings: {
    warming: "Semantic search is still loading its local model — try again in a few seconds.",
    unavailable: "Semantic search is unavailable on this server (the embedding model failed to load).",
  },
  audio: {
    warming: "Transcription is still loading its local model — try again shortly.",
    unavailable: "Audio/video transcription isn't installed on this server.",
  },
  geocoder: { unconfigured: "Address lookup is disabled (no geocoder configured)." },
  push: { unconfigured: "Push notifications aren't configured on this server." },
};
```

### B2. Provider + hooks (`web/src/capabilities.tsx` — new)

Note: there is **no `AuthContext.tsx`** — auth is the inline `AuthCtx` in
`App.tsx:47`, `useAuth` at `:48` (v1 mis-said "AuthContext"; corrected). Mount
`CapabilitiesProvider` **inside the authed branch**, wrapping `<Shell>`
(`App.tsx:129`). It owns the manifest + poll + observed-feed subscription, and
exposes:

```ts
export function useCapabilities(): {
  manifest: CapabilitiesManifest | null;
  health: ServerHealth;
  lastOkAt: number | null;
  refresh: () => void;
};
export function useCapability(id: CapId): {
  ready: boolean; state: CapState;
  reason: string | null;          // CAP_COPY[id][state] (or detail); null when ready
  providers?: { anthropic: boolean; xai: boolean };
};
```

Poll loop (§d for cadence): never clears auth on non-401 (constraint:
offline-tolerant; preserves `App.tsx:106`); bubbles 401 to the App handler;
keeps the last manifest on a blip (sticky — a transient outage doesn't disable
the whole UI). Backward-compat: keep `hasLlm` on `AuthCtx`, derived from the
manifest, so the single existing consumer (`Attachments.tsx:38`) needs no change
until Phase 3.

### B3. Three shared primitives (`web/src/components/Capability.tsx` — new)

```tsx
<RequiresCapability id="embeddings" mode="hide" | "disable" | "note">…</RequiresCapability>
<CapabilityButton cap="llm" onClick={…}>Analyze with AI</CapabilityButton>
<CapabilityNote id="embeddings" />
```

`CapabilityButton` reads `useCapability(cap)`; when `!ready` it sets `disabled`,
`title={reason}`, and `aria-disabled` (a11y, kept). `warming` renders disabled
with a subtle spinner + "available shortly" tone; `unavailable`/`unconfigured`
render with a permanent danger tone. This warming-vs-permanent distinction is
what keeps the UX honest rather than punitive (red-team praised it).

---

## C. RE-VERIFIED feature → capability inventory (the heart)

Method: every route in `App.tsx:124-156` plus every action control inside those
pages/components, each re-opened and line-checked on 2026-06-08. **Anchors below
are verified exact.** "Degrade" is built from the §B3 primitives.

### Capture / Chat (`web/src/pages/Chat.tsx`)

| Entry point | Anchor (verified) | Requires | Degrade |
|---|---|---|---|
| Mode **Entry** (Generic/Medical/Financial) | `MODES` `:55-59`; seg render `:858-876`; `online` `:117,505,922` | online only | Already gated on `!online`. **No LLM** — keep fully usable with no key. |
| Mode **Research** | seg `:858-866`; mode used in send `:519,669` | `llm.ready` + online | Gate the seg cell via `CapabilityButton`-style disable; if selected while `llm` unconfigured, replace the safety line (`:928`) with `CapabilityNote("llm")` and disable Send. |
| Mode **Full Brain** | seg `:858-866`; `:669` (`"assisted"`) | `llm.ready` + online | Same as Research. |
| Research **Deep** toggle | `:939-941` | `llm.ready` | Lives inside the Research mode → covered transitively (hidden unless Research selected). |
| **Attach file** | `:943-945` (shown when `mode!=="research"`) | online (attach is local) | No LLM gate — keep available. |
| **Send** | `:946-947` (`disabled=` expr `:947`) | online + (llm if `mode!=="entry"`) | Add `(mode!=="entry" && !llmReady)` to the existing `disabled`; safety line explains. |
| Medical/Financial dest loads | `:329-343` | online | Already tolerant (empty picker offline). |
| **Lab extraction** on Medical PDF upload | `extractLabs` `:558-562` | `llm.ready` | Already best-effort try/catch; add `CapabilityNote("llm")` near the Medical sub (`:879-890`) so the user knows extraction won't run with no key. |
| Research external-lookup approve/skip | `approveProposal`/`skipProposal` `:713-727`; buttons `:826-827` | `llm.ready` (inside Research) | Covered transitively by the Research mode gate; buttons already disable on `streaming/busy`. Documented as transitive. |

### Search (`web/src/pages/SearchPage.tsx`) — corrected per 1a/1c

Live-as-you-type (debounce 200ms, `:71-82`); **no submit button** (`:85`);
`MODES` at `:36`; default `hybrid` (`:44`); errors swallowed (`:79`). Mode
buttons at `:93-97`.

| Entry point | Anchor | Requires | Degrade |
|---|---|---|---|
| **keyword** mode | `:36,93-97` | none (FTS) | Always available — the safe default. |
| **semantic** mode | `:36,93-97` | `embeddings.ready` | **Disable the `semantic` mode button** (`CapabilityButton` tooltip = embeddings copy). If `semantic` is the active mode and embeddings aren't `ready`, **force `hybrid`** before the next query so we never fire a failing semantic request on every keystroke (1c). |
| **hybrid** mode | `:36,44,93-97` | embeddings *preferred* | Selectable even while `warming`; now runs keyword-only safely (§a0 fix). Show a small "(keyword only — semantic loading)" note via `CapabilityNote` when embeddings `!ready`. |
| **entities** mode | `:36,93-97` | entity index (always present) | No gate. |

Because the page queries on every keystroke, the gate must **force keyword/hybrid
when embeddings aren't `ready`** (Plan A's principle) — the toggle disable alone
isn't enough if the URL seeds `mode=semantic` (`:43-44`). Also: the swallowed
`catch` at `:79` stays swallowed (do **not** wire the §e toast here, or a cold
boot = a keystroke-rate toast storm — special-cased).

### Attachments (`web/src/components/Attachments.tsx`) — generalize the existing gate

| Entry point | Anchor | Requires | Degrade |
|---|---|---|---|
| **Analyze with AI** (image) | `:290` (`hasLlm && isImage…`) | `llm.ready` | Already hidden when `!hasLlm`; migrate to `<RequiresCapability id="llm" mode="hide">`. |
| **Transcribe** (audio/video) | `:285` (comment: "no LLM key required") | `audio.ready` | Currently always shown. Gate on `audio`: `warming` → disabled "loading model…"; `unavailable` → `CapabilityNote("audio")`. (Transcribe is a queued background task `:133-135`, so this is "warn before use," not a blocking call.) |
| **Video** transcribe → vision summary | server-side `audio_transcription.py:255` (`has_credentials()`) | `llm.ready` | New row (4.4). When transcribing a **video** with `llm` unconfigured, add a `CapabilityNote("llm")`: "Transcript only — the visual summary needs an AI key." |
| Help copy line | `:197` (`hasLlm ? "summarized by AI…"`) | `llm` | Switch the conditional from `hasLlm` to `useCapability("llm").ready`. |

### Note page (`NotePage.tsx`, `AiAnalysisPanel.tsx`, `RebuildPanel.tsx`, `TalkPanel.tsx`)

| Entry point | Anchor | Requires | Degrade |
|---|---|---|---|
| **AI Analysis** re-analyze (↻) | `AiAnalysisPanel.tsx:63` (`reanalyze`→`refreshNoteAnalysis` `:44`); panel mounted `NotePage.tsx:219` | `llm.ready` | Render ↻ as `CapabilityButton cap="llm"`; when unconfigured, show the read-only sidecar (if any) + `CapabilityNote("llm")`. |
| **Rebuild / Draft / Regather / Guide / Redraft** | `RebuildPanel.tsx:7` (stream fns), buttons `:259-292`; panel mounted `NotePage.tsx:372` | `llm.ready` (+ `embeddings` for gather quality) | Gate the entry control that opens RebuildPanel on `llm`. The panel's `searchRebuildSources` gather now degrades to keyword safely (§a0); add `CapabilityNote("embeddings")` when embeddings `!ready` so results are honestly "keyword-only." |
| **TalkPanel** (KB notes) | `NotePage.tsx:221` (KB only) | `llm.ready` | Gate its send control with `CapabilityButton cap="llm"`. (This is the **only** note-embedded chat — `GuidedChat`/`ResearchChat` are NOT here; see Share rows.) |

### Public share (`web/src/pages/SharePage.tsx`) — NEW section, server-driven (1b)

These run **outside** the Capabilities provider; gating is server-driven via the
landing flag (§a2), read from the unauthed `getShare` payload
(`SharePage.tsx:52`).

| Entry point | Anchor | Requires | Degrade (server-driven) |
|---|---|---|---|
| **Guided** intake chat | `SharePage.tsx:66-69` → `GuidedChat` | `llm` (server) | If landing `llm_ready === false`, SharePage renders "This assistant is temporarily unavailable — please check back later" instead of `GuidedChat`. Backstop: `start`/`turn` 404 (`share.py:192,292`). |
| **Research** Q&A chat | `:72-74` → `ResearchChat` | `llm` (server) | Same. |
| **Encrypted chat** (`kind="chat"`) | `:77-78` → `ChatShareGuest` | **none** | E2EE human↔human, **no LLM** (`share.py:527-548`). No gate. |
| **Labs** share view | `:82-85` → `LabShareView` | none (data view) | No gate. |
| Note view / Suggest-an-edit | `:150-205` | none | No gate. |

### Advanced launcher cards (`web/src/pages/AdvancedHome.tsx`) — anchors re-confirmed exact

Cards are navigation; default is **navigate-then-explain at the destination**.

| Card | Anchor | Whole-page need | Degrade |
|---|---|---|---|
| Wiki `:14` / Lists `:15` / Calendar `:16` / Graph `:18` / Users `:21` / Prompts `:29` / Data·SQL `:38` | — | none | — |
| **Search** `:17` | — | none (keyword baseline) | Card always opens; semantic gated inside (above). |
| **Entities** `:19` | — | view: none | **Corrected (1e):** no standalone rebuild button exists. Identity edits (merge/split/alias, `entities.py:40-66`) defer a rebuild that needs llm+embeddings. Add a `CapabilityNote` near the identity controls when `llm`/`embeddings` `!ready` ("the rebuilt entity won't get a KB article / vector until an AI key + embeddings are available"); surface `last_error` from the existing `/status` poll (`EntitiesPage.tsx:96`). |
| **Map** `:20` | — | none | **Corrected (1d):** removed the no-op geocoder gate. Trail/heatmap/labels render regardless (labels are pre-resolved, `MapPage.tsx:212`). |
| **Medical** `:22` | — | view: none | View always; lab-extraction controls gated (Chat capture + LabImportPanel rows). |
| **Labs** `:23` | `LabImportPanel.tsx:58` | view: none | **Corrected (1f):** gate the **"Extract lab values"/"Re-analyze"** button (`reanalyzeLabs`, llm-backed `lab_vision.py:86`) with `CapabilityButton cap="llm"`. There is no "AI import" button. |
| **Actions** `:30` | — | LLM at run time | Editing always; note that recipes invoking AI need a key. |
| **Triggers/Flows** `:31` (`WorkflowsPage.tsx`) | — | LLM/embeddings at run time | Editing always; per-trigger note when its action needs a missing cap. |
| **Shares** `:37` | — | none | **Corrected (4.2):** owner-side share management + `OwnerChatPage` are **not** LLM (E2EE chat). No gate. (Guided/research links' recipient experience is gated server-side, §Share rows.) |
| **System** `:39` | — | none | Hosts the health/capabilities panel (§d). |

### Cross-cutting (always-mounted)

| Entry point | Anchor | Requires | Degrade |
|---|---|---|---|
| **ModelPicker** missing-key warning | re-fetch `:37`, compute `:48-52`, render `:61-66` | `llm.providers` | Replace its private `/verify` re-fetch with `useCapability("llm").providers`; keep its (already-good) per-provider copy. Removes a duplicate poll. |
| **ReviewBell / push** | `Shell.tsx:15-60` | `push.ready` | Already feature-detects browser support; add a one-line note when server `push` `unconfigured`. |
| **Offline banner** | `Shell.tsx:261` (`!online`, `:172`) | network | Augmented by the real server-reachability dot (§d). |

This table is the maintained artifact. A checklist comment at the top of
`AdvancedHome.tsx` and `capabilities.ts` points here.

---

## D. Server/API health indicator — real & real-time (rebuilt)

`/api/capabilities` is the declared signal; the observed-traffic feed is the
between-polls signal. Together they make the dot truthful in real time.

### d1. Poll (adaptive cadence — from Plan A)

A single `setInterval` while `document.visibilityState === "visible"`:
**5s while any subsystem is `warming`, 20s steady**; immediate refresh on
`focus`/`pageshow`/`online` (mirror `Shell.tsx:49-51`, incl. `pageshow` which
v1 omitted and mobile/PWA needs). Add an **AbortController timeout** (~8s, from
Plan B) so a dead VM flips the dot promptly instead of hanging on TCP. Keep the
cadence in a ref so a `warming→ready` transition doesn't thrash the interval
(the churn Plan A's red-team flagged).

### d2. Observed-traffic feed (from Plan D — no SSE)

A dependency-free module `web/src/health.ts` (singleton bus). Instrument the
**single chokepoint** `api()` (`api.ts:40-57`) — wrap the `fetch` in try/catch:
- `res.status >= 500` → mark server `degraded`, stamp `last5xxAt`;
- `res.status < 500` (incl. 401/4xx) → server is answering → `lastOkAt = now`;
- thrown network error → `lastNetErrAt`, mark suspect, trigger an immediate
  `/api/capabilities` re-fetch.
- **401 still throws unchanged** → `App.tsx:106` untouched (offline-tolerant
  auth preserved).

Also instrument `streamChat` (`api.ts:712+`; note there is **no** outer
try/catch around its initial `fetch` at `:735` — one must be *added*, not
"instrumented" — Plan D's red-team caught this) and the stall-watchdog
(`api.ts:752`, 90s) → `{kind:"stall"}` → suspect + immediate snapshot. Reuse the
existing reader-loop `catch` sites in `openChatStream` (`:245,256`) and
`streamSSE` (`:821`).

Reconciliation precedence (from Plan D): **declared/polled state wins**; observed
can only *downgrade* transiently and self-heals on the next successful poll/turn;
observed never *upgrades*.

### d3. `ServerHealth`

- poll ok → `ok`; any capability `unavailable` → `degraded`;
- recent neterr/stall AND no server byte within ~8s → `unreachable` (keep last
  manifest, **don't log out**);
- before first response → `unknown`.

### d4. UI

1. **Shell status dot** beside the brand (`Shell.tsx:240`, next to `ReviewBell`
   `:243`): green = ok, amber = degraded/warming, red = unreachable, grey = unknown.
   `title` e.g. "Server reachable · semantic search loading." Replaces relying on
   `navigator.onLine` alone; `useOnline` (`hooks.ts:264`) stays for the true
   browser-offline banner (`Shell.tsx:261`). Three-axis distinction (browser-
   offline vs server-unreachable vs subsystem-degraded) — the model the goal asks
   for.
2. **Reachability banner** when `unreachable` for > one poll: "Can't reach
   <brain> — showing cached data" with "last seen Ns ago" from `lastOkAt`
   (distinct from the browser offline banner; both can coexist).
3. **System page panel** (`SystemPage.tsx`): per-capability `state` + `detail`,
   server version/uptime — the manifest made visible. Reuses `/api/capabilities`
   (no extra call) + existing `/system/stats`.

All cross-origin-safe (ordinary bearer fetch; CORS `*` + bearer, `main.py:232-242`).

---

## E. In-the-moment error surfacing (first-class backstop, not deprioritized)

The red-team's 3a/5 point: the frontend-centric thesis *depends on* this layer,
so v2 treats it as an equal partner.

1. **~80-line dependency-free toast** (`web/src/components/Toaster.tsx` +
   `useToast()`), mounted once near Shell. Fed by the same `health.ts` bus.
2. **`ApiError.category`** (`api.ts:59-65`): `"auth"|"network"|"unavailable"|
   "validation"|"server"` inferred from status (the wrapper already centralizes
   parsing at `:46-53`).
3. **`explainError(err, capHint?)`**: on a 503/feature failure, consult the live
   manifest and produce the *same* copy the gate would have, not a raw `detail`.
   Wire into the `alert()` sites (`Chat.tsx:514-515,934`) and RebuildPanel.
   **Explicitly excluded:** SearchPage's `:79` keystroke catch (toast storm).
4. **No backend envelope rework.** Optional later: feature endpoints raise `503`
   with a known detail so `explainError` is exact without consulting the manifest.

---

## F. Constraints (research §8) — respected

1. **Offline-tolerant auth** — poller/observed-feed never clear the key on
   5xx/network/stall; only the existing `App.tsx:106` 401 path logs out. Manifest
   sticky across blips.
2. **Cross-origin** — manifest, poll, observed-feed are ordinary bearer fetches
   via `u()`/`authHeaders`; the public-share flag rides the existing unauthed
   `getShare` (`publicApi`, same-origin cookie, `api.ts:131`).
3. **Cost** — `llm` readiness is key-presence only; never a live model call. The
   §a0 bug fix removes a hang, not adds work.
4. **Cheap & frequent** — adaptive 5s/20s, paused when hidden, 8s abort;
   `/api/capabilities` reads flags/config only; observed-feed is free (piggybacks
   real traffic).
5. **Graceful degradation** — search forces keyword when embeddings aren't ready
   *and* the server now actually degrades; `warming` vs `unavailable` keeps "try
   again" honest; never gate local/offline-safe actions (Entry capture, keyword
   search, attach, E2EE share chat).
6. **No new heavy deps** — hand-rolled toast + bus; no SSE/WS (deliberately not
   Plan D's stream — overkill for one user).
7. **Security** — `/api/capabilities` key-gated; the public-share flag leaks only
   "the owner's assistant is up," no more than the recipient learns by trying.

---

## Ordered phases

- **Phase 0 — Backend.** (a0) `search.py` semantic try/except;
  `embeddings.readiness()`/`audio_transcription.readiness()` + flags;
  `routers/capabilities.py` + register (`main.py:244`); `llm_ready` into the two
  share landings (`share.py:163,261`). Tests + curl.
- **Phase 1 — FE context + primitives.** `capabilities.ts/.tsx`,
  `components/Capability.tsx`; mount provider inside the authed subtree
  (`App.tsx:129`); derive `hasLlm` from manifest (no consumer change).
- **Phase 2 — Real-time health.** `health.ts` bus; instrument `api()`
  (`:40-57`) + `streamChat`/stall + reader loops; adaptive poll w/ 8s abort;
  Shell dot + reachability banner; System panel. Migrate `Attachments.tsx` +
  `ModelPicker.tsx` to the hooks (proves the primitives + removes the duplicate
  `/verify` poll).
- **Phase 3 — Exhaustive gating sweep.** Walk §C top-to-bottom: Chat
  modes/Send/lab-note, SearchPage force-keyword + semantic disable, NotePage
  AI/Rebuild/Talk, Attachments transcribe + video-vision note, Labs Extract
  button, Entities identity-edit note, SharePage server-driven landing.
- **Phase 4 — Error backstop.** Toaster + `useToast`; `ApiError.category`;
  `explainError`; rewire `alert()` sites (exclude SearchPage `:79`).
- **Phase 5 — Drift guards & docs.** Checklist comments; the exhaustiveness
  test (below); this doc linked from code.

---

## Testing strategy

- **Backend:** `search()` returns keyword/entity hits (no hang/500) when
  `_get_model` raises/blocks (monkeypatch); `readiness()` → `unknown` pre-warm,
  `warming` after start, `ready` after load, `unavailable` on import flag, and
  **audio `warming` again after a `_model_key` change** (the reload bug);
  `/api/capabilities` shape + 401 without key + asserts `_model is None` after a
  call (no model load); guided/research landing includes `llm_ready` and the
  `start`/`turn` 404 when false.
- **Frontend (vitest):** `useCapability` maps each `state` → `ready`/`reason`;
  `CapabilityButton` sets `disabled`/`title`/`aria-disabled` for
  `warming`/`unavailable`/`unconfigured`; provider keeps last manifest + sets
  `health="unreachable"` on a failed poll **without** clearing auth; observed-feed
  flips the dot on a 5xx between polls and self-heals; SearchPage forces
  keyword/hybrid when embeddings `!ready` and fires **no** semantic request;
  401 still logs out, 5xx/neterr never do.
- **Copy-exhaustiveness test (kept + extended):** snapshot asserting every
  `CapId` has `CAP_COPY` for each of its *reachable* states; **extended** to also
  assert every `CapId` referenced in `CAP_COPY`/the inventory exists in the
  `Capability` manifest type (catches a new capability with no copy *and* a copy
  key with no capability).
- **Integration / manual matrix:** no LLM key → Research/Full disabled + copy,
  Entry usable, guided-share landing shows "unavailable"; embeddings
  `unavailable` → semantic disabled, hybrid runs keyword-only, no toast storm;
  mid-session 5xx → dot amber within ~1 request (not 20s); server stopped →
  reachability banner, no logout; cross-origin (Pages → VM); tab hidden → poll
  pauses; cold boot → `warming` resolves to `ready` within one 5s tick.

---

## Risks & tradeoffs (honest)

1. **Drift — the central risk.** Exhaustive manual gating means each new
   AI-consuming surface *should* add a row + a gate, and nothing enforces it at
   the framework level. v2 strengthens mitigations beyond v1's comments: the
   extended exhaustiveness test, the primitives making a gate ~1 line, **and (per
   the red-team) treating §e as a first-class equal partner** so an ungated
   surface still surfaces a real error. A future stronger guard: a CI check that
   the inventory doc lists every `App.tsx` route.
2. **Manifest vs reality skew.** `llm.ready` = key present, not valid (cost). A
   revoked key passes the gate and fails at call time — caught by the observed
   feed (downgrade) + §e. Accepted; proving validity burns tokens.
3. **Between-polls race — now bounded.** The observed-feed + 5s-while-warming +
   8s-abort + immediate-on-focus shrink v1's ≤20s window to ~one failing request.
4. **`warming` flicker on cold boot** — correct, not a bug; the "try again
   shortly" tone (distinct from `unavailable`) prevents it reading as broken.
5. **Public-share gate is server-trust, not client-context** — by necessity
   (the route has no provider). The landing flag + the existing `start`/`turn`
   404 are the only honest options; both are server-authoritative, which is fine.
6. **Single-process readiness flags.** Per-process/in-memory; JBrain is
   single-process today (no `--workers`, one uvicorn worker — verified by Plan A's
   red-team). Latent only; documented so nobody adds workers without a shared
   store.

---

## Critical files

- `server/app/routers/search.py` (`:80-92` semantic try/except — the real bug fix)
- `server/app/routers/capabilities.py` (new) + `server/app/main.py` (`:244` register; warmups `:180,211`)
- `server/app/services/embeddings.py` (`readiness()` + flags, `:20-30`) and `services/audio_transcription.py` (`readiness()` keyed on `_model_key`, `:93-110`)
- `server/app/routers/share.py` (`llm_ready` into `_guided_landing` `:163` / `_research_landing` `:261`)
- `web/src/capabilities.ts` + `web/src/capabilities.tsx` + `web/src/components/Capability.tsx` (new: types/copy, provider/hooks, primitives)
- `web/src/health.ts` (new observed-traffic bus) + `web/src/api.ts` (`:40-57` instrument `api()`; `:712,752` streamChat/stall) + `web/src/components/Toaster.tsx` (new)
- `web/src/App.tsx` (`:129` provider mount; derive `hasLlm`; preserve `:106`) + `web/src/components/Shell.tsx` (`:240` dot, `:261` banner)
- Gated surfaces: `web/src/pages/Chat.tsx`, `pages/SearchPage.tsx`, `pages/SharePage.tsx`, `components/Attachments.tsx`, `components/AiAnalysisPanel.tsx`, `components/RebuildPanel.tsx`, `components/TalkPanel.tsx`, `components/LabImportPanel.tsx`, `pages/EntitiesPage.tsx`, `components/ModelPicker.tsx`
