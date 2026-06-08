# Plan C — Capability-Manifest-Driven UI Gating (frontend-centric)

**Author:** Architect C
**Philosophy:** Never let a user start something that can't succeed. A single
typed capabilities manifest, supplied honestly by the backend, is consumed by
the whole frontend through a `Capabilities` context + `useCapability()` hook and
a small set of shared gating primitives. Every feature entry point in the PWA is
mapped to the capability/subsystem it needs and given a consistent
disabled-with-reason state. The server/API health indicator is real but
deliberately lightweight; in-the-moment error surfacing is the fallback for
anything that slips past gating.

This plan treats **exhaustive, consistent pre-flight gating** as the heart of
the work. The backend manifest is intentionally lean.

---

## 0. Verified ground truth (checked against code)

The research doc (`00-research.md`) is accurate. Confirmations from the code:

- `GET /api/health` returns only `{ok, brain}` — pure liveness
  (`server/app/main.py:254-256`).
- The de-facto manifest is `GET /api/auth/verify`
  (`server/app/routers/auth_router.py:22-38`): returns `has_llm`, `app_tz`,
  `owner_set`, `llm_keys:{anthropic,xai}`, `vapid_public_key`, `version`.
- Capabilities are config-derived in `server/app/config.py:70-81`
  (`has_anthropic`, `has_xai`, `has_llm`). Geocoder presence is `geocoder_url`
  (`config.py:39`, consumed by `services/geocode.py`).
- **Embeddings readiness is not exposed.** Lazy `_get_model()` under a lock
  (`services/embeddings.py:20-30`); warmed async on boot
  (`main.py:177-202`, `asyncio.create_task(_warm_embeddings())`).
- **Audio/transcription readiness is not exposed.** Lazy `_get_model()`
  (`services/audio_transcription.py:93-110`) raises `TranscriptionUnavailable`
  if `faster_whisper` is missing; warmed async, best-effort
  (`main.py:208-215`).
- Frontend auth context is thin (`web/src/App.tsx:33-48`): only `hasLlm`,
  `appTz`, `vapidPublicKey`, version fields. **`hasLlm` is consumed in exactly
  ONE place** — `web/src/components/Attachments.tsx:38,197,290`.
- The chat composer already gates on `!online` (`web/src/pages/Chat.tsx:505,
  921-922,947`) but **not** on `has_llm` for Research/Full Brain modes.
- `ModelPicker.tsx:30-66` re-fetches `/api/auth/verify` itself for `llm_keys`
  and already warns when a selected model's provider key is missing — a pattern
  to generalize.
- Offline-tolerant auth: only a real 401 clears the key (`App.tsx:97-110`).
- Central API wrapper `api<T>()` (`web/src/api.ts:40-57`) throws `ApiError`;
  there is no toast system; user-action errors use blocking `alert()`
  (`Chat.tsx:480`, etc.); many loads `.catch(() => {})`.
- Status bar today: version-mismatch banner + `navigator.onLine` offline banner
  (`web/src/components/Shell.tsx:174,258-261`), via `useOnline`
  (`web/src/hooks.ts:264-277`).

---

## (a) Minimal backend manifest

### A1. New endpoint: `GET /api/capabilities` (key-gated)

One consolidated, **cheap** readiness manifest. Key-gated (same auth posture as
`/verify`); no pre-auth capability leak (constraint 8/security). It must do no
heavy work per call — it only reads cached process flags + cheap config props.

```jsonc
// GET /api/capabilities  (CurrentUser)
{
  "server": {
    "version": "1.42.0",
    "app_tz": "America/New_York",
    "owner_set": true,
    "uptime_seconds": 3600,
    "ts": "2026-06-07T15:04:05Z"      // server clock, for skew-tolerant freshness
  },
  "capabilities": {
    "llm":        { "ready": true,  "state": "ready",
                    "providers": { "anthropic": true, "xai": false },
                    "detail": "Claude key present" },
    "embeddings": { "ready": false, "state": "warming",
                    "detail": "Local model still loading" },
    "audio":      { "ready": true,  "state": "ready",
                    "detail": "faster-whisper warmed" },
    "geocoder":   { "ready": true,  "state": "ready", "detail": null },
    "push":       { "ready": true,  "state": "ready", "detail": null }
  }
}
```

`state` is one of: `"ready" | "warming" | "unavailable" | "unconfigured"`.
`ready` is the boolean shorthand (`state === "ready"`). The richer `state`
lets the UI distinguish *"loading, try shortly"* (`warming`) from *"will never
work here"* (`unavailable`/`unconfigured`) — central to honest gating.

Semantics per capability:

| Capability | `ready`/`state` derived from | Cost note |
|---|---|---|
| `llm` | `state=ready` iff `has_llm`; else `unconfigured`. `providers` = `{has_anthropic, has_xai}`. | Key **presence only** — never a live model call (constraint: cost). |
| `embeddings` | `unavailable` if `fastembed` import failed; `ready` if `_model is not None`; `warming` otherwise. | Reads a flag; no model load. |
| `audio` | `unavailable` if `faster_whisper` missing; `ready` if `_model is not None`; `warming` otherwise. | Reads a flag. |
| `geocoder` | `ready` iff `geocoder_url` non-empty, else `unconfigured`. | Config only. |
| `push` | `ready` iff a VAPID public key exists. | Config/DB meta. |

### A2. Readiness flags in the lazy services (the one real backend change)

To answer `embeddings`/`audio` without doing work, expose a non-blocking
readiness probe. Add to `services/embeddings.py`:

```python
_import_ok = True   # set False if `from fastembed import TextEmbedding` ever ImportErrors

def readiness() -> str:
    """Cheap, non-blocking: 'ready' | 'warming' | 'unavailable'."""
    if not _import_ok:
        return "unavailable"
    return "ready" if _model is not None else "warming"
```

Set `_import_ok = False` inside `_get_model()`'s import on `ImportError`
(mirroring the audio service's existing `TranscriptionUnavailable` path).
Add the analogous `readiness()` to `services/audio_transcription.py`
(it already raises `TranscriptionUnavailable` on missing dep — reuse that to
flip an `_import_ok` flag the first time `_get_model()` is attempted; until then
report `warming`). The boot warmups (`main.py:202,215`) already call
`_get_model()`, so on a healthy box these flip to `ready` within seconds of
boot with no extra work.

A new router `server/app/routers/capabilities.py` assembles the manifest from
`get_settings()` props + `embeddings.readiness()` + `audio_transcription.readiness()`
+ `push.public_key()`. Register it in the `main.py:244` router loop.

### A3. Freshness for "real-time" feel

No new transport/deps. The PWA **polls `/api/capabilities`** on a single shared
interval (see C-context below): every **20s** while the tab is visible, paused
when hidden (`visibilitychange`), and re-fetched immediately on
`focus`/`pageshow`/`online`. Because `warming → ready` is the only transient
that matters and it resolves seconds after boot, 20s is ample; it is also cheap
(reads flags). This piggybacks on the existing health poll (see (d)) — **one
request** serves both health and capabilities (the manifest's presence *is* the
"server reachable + authed" signal). This keeps it lightweight (constraint:
cheap & frequent) and cross-origin safe (constraint: cross-origin deploys — it's
a normal bearer fetch, same as `/verify`).

---

## (b) Frontend Capabilities context + hooks + shared primitives

### B1. Types (`web/src/capabilities.ts` — new)

```ts
export type CapState = "ready" | "warming" | "unavailable" | "unconfigured";
export type CapId = "llm" | "embeddings" | "audio" | "geocoder" | "push";

export interface Capability {
  ready: boolean;
  state: CapState;
  detail: string | null;
  providers?: { anthropic: boolean; xai: boolean };  // llm only
}

export interface CapabilitiesManifest {
  server: { version: string; app_tz: string; owner_set: boolean;
            uptime_seconds: number; ts: string };
  capabilities: Record<CapId, Capability>;
}

// Server reachability is orthogonal to per-capability readiness.
export type ServerHealth = "ok" | "degraded" | "unreachable" | "unknown";

// Human-readable copy keyed by capability + state. ONE source of truth so every
// gated surface explains itself identically. (constraint: graceful degradation.)
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

### B2. Provider + hook (extend, don't replace, AuthContext)

Rather than fork two contexts, **fold capabilities into the existing auth flow**
so there is one provider tree. Add a `CapabilitiesProvider` that wraps the
authed subtree (mounted in `App.tsx` just inside the authed branch, around
`<Shell>` — `App.tsx:128-160`). It owns the manifest + the poll loop and exposes:

```ts
// web/src/capabilities.tsx (new) — context + hooks
export function useCapabilities(): {
  manifest: CapabilitiesManifest | null;
  health: ServerHealth;
  lastOkAt: number | null;     // for "last seen 12s ago" UI
  refresh: () => void;
} { ... }

// The workhorse hook every gated surface calls:
export function useCapability(id: CapId): {
  ready: boolean;
  state: CapState;
  reason: string | null;       // CAP_COPY[id][state] (or detail), null when ready
} { ... }
```

The poll loop:
- single `setInterval(20000)` while `document.visibilityState === "visible"`,
- immediate refresh on `focus`/`pageshow`/`online`,
- on a non-401 failure: **do not** clear auth (constraint: offline-tolerant) —
  set `health` to `unreachable`/`degraded` and keep the last manifest
  (capabilities are sticky so a blip doesn't disable the whole UI),
- on 401: bubble to the existing App-level handler (no behavior change).

Backward compatibility: keep `hasLlm` on `AuthContext` for now, but derive it
from the manifest so the single existing consumer (`Attachments.tsx`) needs no
change yet; migrate it to `useCapability("llm")` in Phase 2.

### B3. Shared gating primitives (`web/src/components/Capability.tsx` — new)

Three primitives, used everywhere (DRY = the antidote to drift):

```tsx
// 1) Wrapper: render children only when ready; otherwise render a fallback note.
<RequiresCapability id="embeddings" mode="hide" | "disable" | "note">
  {children}
</RequiresCapability>

// 2) Button that disables itself with an explained tooltip/inline reason.
<CapabilityButton cap="llm" onClick={...} className="primary">
  Analyze with AI
</CapabilityButton>
// → when not ready: rendered disabled, title={reason}, with an inline
//    <CapabilityNote> beneath when `explain` is set.

// 3) Inline explainer chip (the consistent "why + what to do" line).
<CapabilityNote id="embeddings" />   // renders CAP_COPY copy in a muted/danger style
```

`CapabilityButton` is the most-used primitive: it wraps a normal `<button>`,
reads `useCapability(cap)`, and when `!ready` sets `disabled`, a `title` tooltip,
and `aria-disabled` (a11y). `warming` states render as disabled with a subtle
spinner affordance and an "available shortly" tone, distinct from the permanent
`unavailable`/`unconfigured` (danger tone). This single distinction is what
makes the UX honest rather than punitive.

---

## (c) EXHAUSTIVE feature → capability inventory (the heart)

Method: every route in `App.tsx:132-156` plus every action-triggering control
inside those pages/components, mapped to the capability it needs and the chosen
degradation. "Degrade" column states the *exact* UX, all built from the B3
primitives so they are consistent.

### Capture / Chat (`web/src/pages/Chat.tsx`)

| Entry point | Anchor | Requires | Degrade |
|---|---|---|---|
| Mode: **Entry** (Generic/Medical/Financial) | `Chat.tsx:55-66,537-575` | online only | Already gated on `!online`. No LLM needed — keep fully available even with no key. |
| Mode: **Research** | `Chat.tsx:57,669` | `llm.ready` + online | Disable the mode segment via `CapabilityButton`-style gating on the seg cell; if selected while `llm` unconfigured, replace composer safety line with `CapabilityNote("llm")` and disable Send. Tooltip: the LLM copy. |
| Mode: **Full Brain** | `Chat.tsx:58,669` | `llm.ready` + online | Same as Research. |
| Research **Deep** toggle | `Chat.tsx:939-941` | `llm.ready` | Hidden when Research is gated (it lives inside a gated mode). |
| **Attach file** button | `Chat.tsx:943-945` | online (attach is local) | No LLM gate (transcription/analysis happen server-side, best-effort). Keep available. |
| **Send** | `Chat.tsx:946-947` | online + (llm if chat mode) | Add `mode!=="entry" && !llmReady` to the existing `disabled` expression; safety line explains. |
| Medical/Financial **dest** loads | `Chat.tsx:329-343` | online | Already tolerant (empty picker offline). |
| Lab extraction on PDF upload | `Chat.tsx:558-562` (`extractLabs`) | `llm.ready` | Already best-effort/try-catch; add a `CapabilityNote` near the Medical sub when `llm` unconfigured so the user knows extraction won't run. |

### Search (`web/src/pages/SearchPage.tsx`)

| Entry point | Anchor | Requires | Degrade |
|---|---|---|---|
| **keyword** mode | `SearchPage.tsx:36,77` | none (FTS) | Always available — the safe default. If `embeddings` not ready, force-select keyword. |
| **semantic** mode | `SearchPage.tsx:36` | `embeddings.ready` | Disable the `semantic` toggle via gating; tooltip = embeddings copy (`warming` vs `unavailable`). If currently selected and embeddings drop, fall back to `hybrid`/`keyword` and show `CapabilityNote`. |
| **hybrid** mode | `SearchPage.tsx:36` | `embeddings` preferred | Allowed even when embeddings `warming` (server already falls back to FTS), but show a small "(keyword only — semantic loading)" note. |
| **entities** mode | `SearchPage.tsx:36` | entity index (always present) | No gate. |

### Attachments (`web/src/components/Attachments.tsx`) — existing partial gate, generalize

| Entry point | Anchor | Requires | Degrade |
|---|---|---|---|
| **Analyze with AI** (image) | `Attachments.tsx:290` | `llm.ready` | Already hidden when `!hasLlm`; migrate to `<RequiresCapability id="llm" mode="hide">`. Also surface `warming`/`unavailable` for vision. |
| **Transcribe** (audio/video) | `Attachments.tsx:285` | `audio.ready` | Currently always shown (local, no key). Gate on `audio`: when `warming` show disabled "loading model…"; when `unavailable` show `CapabilityNote("audio")` instead of letting it fail. |
| Help copy line | `Attachments.tsx:197` | `llm` for the "summarized by AI" clause | Already conditional on `hasLlm` → switch to `useCapability("llm").ready`. |

### Note page (`web/src/pages/NotePage.tsx`, `AiAnalysisPanel.tsx`, `RebuildPanel.tsx`)

| Entry point | Anchor | Requires | Degrade |
|---|---|---|---|
| **AI Analysis** refresh (↻) | `AiAnalysisPanel.tsx:27-40` (`refreshNoteAnalysis`) | `llm.ready` | Render the ↻ as a `CapabilityButton cap="llm"`; when unconfigured show the read-only sidecar (if any) with a `CapabilityNote`. |
| **Rebuild / Draft / Regather / Guide / Redraft** | `RebuildPanel.tsx:5-9` (all `*Stream`) | `llm.ready` (+ `embeddings` for source gather quality) | Gate the entry button that opens RebuildPanel on `llm`. The panel's `search_notes` gather degrades to keyword if embeddings `warming` (server already handles); add a `CapabilityNote` if embeddings unavailable so results are honestly "keyword-only". |
| Talk / Guided / Research chat embeds | `TalkPanel.tsx`, `GuidedChat.tsx`, `ResearchChat.tsx` | `llm.ready` | Gate their send/start controls with `CapabilityButton cap="llm"`. |

### Advanced launcher cards (`web/src/pages/AdvancedHome.tsx:10-42`)

Cards are navigation, not actions, so the default is **navigate-but-explain at
the destination** rather than blocking the card. Exception: cards whose *entire*
purpose needs a missing capability get a small "needs X" sub-label badge so the
user isn't surprised. Mapping:

| Card | Anchor | Requires (whole-page) | Degrade |
|---|---|---|---|
| Wiki | `:14` | none | — |
| Lists | `:15` | none | — |
| Calendar | `:16` | none | — |
| **Search** | `:17` | none (keyword baseline) | Card always open; semantic gated inside (above). Sub-label note if embeddings `unavailable`. |
| Graph | `:18` | none (reads relations) | — |
| Entities | `:19` | none; **rebuild** action needs nothing extra | Entity rebuild already has its own status poll (`entities/status`); surface its `last_error` via the in-page note. |
| Map | `:20` | `geocoder` for address labels (tiles independent) | Trail/heatmap render regardless; address labels show "coordinates only" note when geocoder `unconfigured`. |
| Users | `:21` | none | — |
| **Medical** | `:22` | none to view; lab extract needs `llm` | View always; extraction controls gated. |
| **Labs** | `:23` (`LabImportPanel.tsx`) | `llm.ready` for AI import | Manual chart view always; **AI import** button gated `CapabilityButton cap="llm"`. |
| Prompts | `:29` | none (config) | — |
| Actions | `:30` | LLM at *run* time | Editing recipes always; a note that recipes invoking AI need a key. |
| Triggers/Flows | `:31` (`WorkflowsPage.tsx`) | LLM/embeddings at run time | Editing always; per-trigger note when its action needs a missing cap. |
| Shares | `:37` | none | Owner-assisted share chat needs `llm` → gate that control only. |
| Data/SQL | `:38` | none | — |
| System | `:39` | none | Hosts the health/capabilities dashboard (see d). |

### Cross-cutting (always-mounted)

| Entry point | Anchor | Requires | Degrade |
|---|---|---|---|
| **ModelPicker** missing-key warning | `ModelPicker.tsx:47-66` | `llm.providers` | Replace its private `/verify` fetch with `useCapability("llm").providers`; keep the existing per-provider warning copy (it's already good). |
| **ReviewBell / push** | `Shell.tsx:15-93` | `push.ready` | Already feature-detects browser support; add a one-line note when server `push` `unconfigured`. |
| Offline banner | `Shell.tsx:261` | network | Augmented by the real server-reachability indicator (d). |

This table is the maintained artifact. A short checklist comment is added at the
top of `AdvancedHome.tsx` and `capabilities.ts` pointing here, so a new
feature's author is reminded to add a row + a gate (drift mitigation, see Risks).

---

## (d) Server/API health indicator (lighter, but real & real-time)

The single `/api/capabilities` poll *is* the health probe (its success ⇒ server
reachable + auth valid; its `state` fields ⇒ subsystem readiness). Derive
`ServerHealth` in the provider:

- fetch ok → `"ok"`; if any capability is `unavailable` → `"degraded"`;
- fetch network/5xx fail → `"unreachable"` (keep last manifest, **don't log
  out**);
- before first response → `"unknown"`.

UI (light treatment):
1. **Shell status dot** beside the brand (`Shell.tsx:240`): green = ok,
   amber = degraded, grey/pulsing = unreachable. `title` shows
   "Server reachable · semantic search loading" etc. This replaces relying on
   `navigator.onLine` alone — `useOnline` stays for the true-offline banner, but
   the dot reflects *our* server (constraint: real reachability, not just
   `navigator.onLine`).
2. **Reachability banner**: when `health === "unreachable"` for > one poll,
   show "Can't reach <brain> — showing cached data" (distinct from the browser
   offline banner; both can coexist). Uses `lastOkAt` for "last seen Ns ago".
3. **System page panel** (`SystemPage.tsx`): a small readiness table listing each
   capability + `state` + `detail`, plus server version/uptime — the full
   manifest made visible for diagnosis. Reuses `/api/capabilities` (no extra
   call) and existing `/system/stats` for storage/tokens.

Cross-origin: all of this is the same bearer fetch already used everywhere
(constraint satisfied).

---

## (e) In-the-moment error surfacing (fallback for gating misses)

Gating is preventive; this is the safety net for anything not gated (e.g. a
capability that flips to `unavailable` mid-session, or a code path with no gate
yet).

1. **Lightweight toast system** (`web/src/components/Toaster.tsx` + a
   `useToast()` hook, ~80 lines, no deps — keep the bundle lean per constraint).
   Mounted once near the Shell.
2. **Enrich `ApiError`** (`api.ts:59-65`) with an optional `category`
   ("auth" | "network" | "unavailable" | "validation" | "server") inferred from
   status (and from a future structured-detail if the backend later adds one).
   The wrapper already centralizes this (`api.ts:46-53`).
3. **Capability-aware error mapping**: a helper `explainError(err, capHint?)`
   that, on a 503/feature failure, checks the live manifest and produces the
   *same* copy the gate would have ("Semantic search is still loading…")
   instead of a raw `detail`. Wire it into the existing `catch` sites that
   currently `alert()` (`Chat.tsx:480-481`, `RebuildPanel`, attachments) and
   into a handful of the silent `.catch(() => {})` loaders so failures become a
   toast rather than nothing.
4. **No backend error-envelope rework required** — this is purely additive on
   the client (keeps backend lean per the philosophy). A later optional
   enhancement: have feature endpoints raise `503` with a known detail when a
   subsystem isn't ready, so `explainError` is exact even without consulting the
   manifest.

---

## (f) Constraints (section 8) — how each is respected

1. **Offline-tolerant auth** — poller never clears the key on 5xx/network; only
   the existing App-level 401 path logs out. Manifest is sticky across blips.
2. **Cross-origin deploys** — manifest/health are ordinary bearer fetches via
   the existing `u()`/`authHeaders()` machinery; CORS already `*` with
   bearer-only (`main.py:232-242`).
3. **Cost** — `llm` readiness is **key-presence only**; never a live model call.
4. **Cheap & frequent** — one 20s visible-only poll, paused when hidden; the
   endpoint reads cached flags/config (no model loads, no DB scans).
5. **Graceful degradation** — gating prefers `disable+explain` and safe
   fallbacks (keyword search, coordinates-only map) over hiding; `warming` vs
   `unavailable` keeps "try again" honest.
6. **No new heavy deps** — toast is hand-rolled; no new runtime deps front or
   back.
7. **Security** — `/api/capabilities` is key-gated; nothing new leaks pre-auth
   (`/auth/info` unchanged).

---

## Ordered phases

**Phase 0 — Backend manifest (small).**
`embeddings.readiness()` + `_import_ok`; `audio_transcription.readiness()` +
`_import_ok`; new `routers/capabilities.py`; register in `main.py:244`. Tests.

**Phase 1 — Frontend context + primitives.**
`capabilities.ts` (types/copy), `capabilities.tsx` (provider + `useCapability` +
poll), `components/Capability.tsx` (the 3 primitives). Mount provider in
`App.tsx` authed subtree. Derive `hasLlm` from manifest (no consumer change).

**Phase 2 — Health indicator.**
Shell status dot + reachability banner; System page readiness panel. Migrate
`Attachments.tsx` and `ModelPicker.tsx` to the new hooks (proves the primitives).

**Phase 3 — Exhaustive gating sweep.**
Walk the inventory table top-to-bottom: Chat modes/Send, SearchPage semantic
toggle, NotePage AI/Rebuild, Labs AI import, Map geocoder note, Advanced
sub-labels. Each = one `CapabilityButton`/`RequiresCapability`/`CapabilityNote`.

**Phase 4 — Error fallback.**
Toaster + `useToast`; `ApiError.category`; `explainError`; rewire the `alert()`
and key silent-catch sites.

**Phase 5 — Drift guards & docs.**
Inventory checklist comments; a lint/test that fails if a new `CapId` lacks
`CAP_COPY`; this doc linked from code.

---

## Testing strategy

- **Backend unit:** `readiness()` returns `warming` before load, `ready` after,
  `unavailable` when the import flag is false (monkeypatch). `/api/capabilities`
  shape + auth-gating (401 without key) + that it does **no** model load
  (assert `_model is None` after a call pre-warmup).
- **Frontend unit (vitest):** `useCapability` maps each `state` → correct
  `ready`/`reason`; `CapabilityButton` disables + sets `title`/`aria-disabled`
  for `warming`/`unavailable`/`unconfigured`; provider keeps last manifest on a
  failed poll and sets `health="unreachable"` without clearing auth.
- **Integration:** boot with no LLM key → Research/Full disabled with copy,
  Entry usable; embeddings `unavailable` → semantic toggle disabled, keyword
  works; mid-session manifest flip to `degraded` → status dot amber + relevant
  controls disable.
- **Exhaustiveness test:** a snapshot test that asserts every `CapId` has
  `CAP_COPY` entries for its reachable states (guards drift).
- **Manual matrix:** cross-origin (PWA on Pages → remote VM), server stopped
  mid-session (banner, no logout), tab hidden (poll pauses).

---

## Risks & tradeoffs (honest)

1. **Maintenance burden / drift — the central risk.** Exhaustive gating means
   every new feature *should* add an inventory row + a gate. Nothing forces this
   at the framework level; a new LLM feature added without a gate silently
   regresses to "let it fail." Mitigations: the shared primitives make adding a
   gate ~1 line; the `CAP_COPY` exhaustiveness test catches a *new capability*
   with no copy; checklist comments point here. But a new *consumer* of an
   *existing* capability can still be forgotten — the error-fallback (e) is the
   backstop, which is exactly why (e) exists despite the frontend-centric thesis.
2. **Manifest vs reality skew.** `llm.ready` means *key present*, not *key
   valid* (deliberate, for cost). A present-but-invalid/expired key still passes
   the gate and fails at call time → caught only by (e). We accept this; proving
   validity would burn tokens on every poll.
3. **Polling latency.** A capability can change up to ~20s before the UI
   notices; a user could start something in that window. Bounded by (e) and by
   the immediate refresh on focus/online. Tighter polling trades cost for
   freshness; 20s is the chosen balance.
4. **`warming` flicker on cold boot.** Right after a server restart, semantic
   search/transcription briefly gate as `warming`. This is *correct* (they
   really aren't ready) but could read as "broken" if copy isn't careful — hence
   the explicit "try again shortly" tone distinct from `unavailable`.
5. **Over-gating annoyance.** Disabling controls can feel heavier-handed than
   letting power users try. Mitigated by preferring `disable+explain` (with a
   visible reason) over hiding, and by never gating local/offline-safe actions
   (Entry capture, keyword search, file attach).
6. **Two-source-of-truth temptation.** Folding capabilities into the auth tree
   (vs a separate context) avoids a parallel provider, but couples lifetimes;
   we keep them logically separate (`useCapability` vs `useAuth`) so a future
   split is mechanical.
7. **Backend leanness vs exactness.** Keeping the backend to a flag-only
   manifest means error copy from (e) sometimes guesses from the manifest rather
   than the failing endpoint. The optional Phase-4 "503 + known detail" upgrade
   closes this if it proves necessary.

---

## Critical Files for Implementation

- `web/src/App.tsx` (provider mount; `hasLlm` derivation; offline-tolerant auth)
- `web/src/pages/Chat.tsx` (highest-value gate: Research/Full Brain + Send)
- `server/app/routers/capabilities.py` (new manifest endpoint) + `server/app/main.py` (register; warmups)
- `server/app/services/embeddings.py` and `server/app/services/audio_transcription.py` (`readiness()` flags)
- `web/src/components/Capability.tsx` + `web/src/capabilities.tsx` (context, hooks, shared primitives)
