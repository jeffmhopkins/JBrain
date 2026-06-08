# Red Team 4 — FINAL round-4 go/no-go review of HYBRID v2

**Reviewer:** Red Team (round 4, final gate) · **Date:** 2026-06-08 · **Target:**
`70-hybrid-v2.md` · **Inputs:** `60-redteam3-hybrid.md` (the 6 findings v2 claims to
fix). **Scope:** backend + frontend as ONE plan, re-verified line-by-line against
the live tree. Legend: ✅ verified-correct · ⚠️ minor/optional · ❌ blocker.

---

## VERDICT (up front): ✅ GO. HYBRID v2 IS READY to be the final/presented plan.

All six round-3 blockers are fixed, and every fix re-checks correct against live
code. The adversarial sweep found **no new blocker**, no regression of the carried
invariants, and no remaining HIGH/MEDIUM. The plan is implementation-ready as
written. A short list of **non-blocking** polish items appears at the end — none
gate presentation.

---

## PART 1 — round-3 fixes re-verified against live code

### H1 — clock fix ✅ CORRECT
`server/app/services/clock.py:51` defines `now_utc() -> datetime: return
datetime.now(_UTC)` (`_UTC = timezone.utc`, `:19`) → an **aware UTC** datetime, so
`.isoformat()` yields `…+00:00`. There is **no** `iso_now` in the module (public
time fns are exactly `app_tz_name:41`, `now_utc:51`, `now_local:55`, `today_*`,
`now_prompt:72`). v2 uses `clock.now_utc().isoformat()` in `_public_skeleton()`
(§3.3) — the single builder both `/status` and folded `/verify` go through. No
AttributeError. The §7 test even asserts `hasattr(clock,"iso_now") is False` to
prevent regression. **Fix is correct and well-guarded.**

### H2 — LLM-ready unification ✅ CORRECT and CONSISTENT
- `llm.has_credentials()` (`services/llm.py:506`) → `get_provider().has_credentials()`
  — the **active-provider** predicate. Per-provider impls verified: Anthropic
  `:142-143` (`bool(get_settings().llm_api_key)`), xAI `:277-278` (`bool(self._key())`).
- The wrong predicate is correctly demoted: `config.py` `has_anthropic:71`,
  `has_xai:75`, `has_llm:80` are **presence-only** properties. v2 keeps the
  `providers:{anthropic,xai}` map as **informational only** (one consumer:
  ModelPicker, §4.7 last row) and never gates on it.
- v2 uses `has_credentials()` consistently for: the capabilities doc
  (`system_status.capabilities().llm.state`, §3.2), the dot (§4.6 reads
  `caps.llm.state`), every feature gate (§4.7 "Every `llm` gate uses the
  `has_credentials()` rollup"), and the share landing (`share.py llm_ready():197-199`
  → `llm.has_credentials()`, verified — **already** the right predicate).
- **No remaining place uses config presence for the "usable" boolean.** I searched
  the plan for `has_anthropic`/`has_xai`/`has_llm` in a gating role: they appear only
  in the informational `providers` map and the legacy `/verify` `llm_keys` field
  (preserved for back-compat, not used by the dot). The provider-mismatch case
  (`LLM_PROVIDER=xai` + only Claude key) now makes dot and share **agree** (both
  read `has_credentials()` → false). **Resolved.**

### M1 — Store reconciliation / one adapter ✅ CORRECT
- ONE adapter `ingestVerify(data)` maps `data.capabilities` for **both** envelopes,
  because §3.4 makes `/verify` and `/status` carry a byte-identical `capabilities`
  sub-shape. No per-envelope branching.
- Seeded from BOTH paths, both verified in live `App.tsx`: `connect()` calls
  `get("/api/auth/verify")` at **:81** (confirmed) and the boot effect calls the same
  at **:101** (`.then` at :102, confirmed). v2 has `connect()` call
  `health.ingestVerify(v)` **and** `health.refreshNow()`; the boot effect calls
  `health.ingestVerify(v)`. The poller calls the SAME `ingestVerify(data)`. Three
  call sites, one adapter, both seeding paths covered. **Resolved.**

### M2 — Stall vs user-abort ✅ CORRECT
Verified the exact ambiguity in live `api.ts`: `streamChat` wires the caller
`signal` at **:731-733**, the watchdog `arm()` at **:753** is bare
`setTimeout(() => ctrl.abort(), STALL_MS)` (`STALL_MS=90000` :752), and the
read-loop catch at **:758-759** is `catch { break; }` — shared by a 90s stall AND a
user abort. v2's fix adds `let stalled=false;` set inside the timeout callback
(`() => { stalled = true; ctrl.abort(); }`) and reports `stall` **only** when
`stalled` is true; a non-stalled aborted read is a benign user cancel → no report.
The initial-fetch catch additionally guards `if (!ctrl.signal.aborted)` before
`neterr`. Same fix applied to `streamSSE` (fetch :806, `arm()` :816, catch :821,
`abort():837`). **The false-stall class is removed entirely.** Correct.

### M3 — needs-auth ✅ CORRECT (skeleton-vs-stored-key, not 401)
Live `auth.py`: `verify_key(:58-64)` returns `False` on empty/bad key and never
throws; `_extract_key(:67-71)` never throws. So on the **soft-auth** `/status`
route a bad/rotated bearer yields the 200 skeleton, and a literal `status===401` is
impossible in normal operation. v2 implements `needs-auth` as **reconciliation rule
0**: `getStatus()` returns `{ok:true,data}` with `data.capabilities === undefined`
(skeleton) AND `getAccessKey()` non-null → store sets `server:"needs-auth"` (amber
"Re-authenticate", never logs out). The helper deliberately does NOT decide it;
it's a store-level rule (§4.1/§4.2). Concrete and consistent across §4.1, §4.2 rule
0, §4.6 dot, and §7 tests. **Resolved.** (Note: `/verify` itself is hard-auth —
`auth_router.py:22` `dependencies=[CurrentUser]` — so a rotated key there still
401s and drives the sole logout at `App.tsx:106`. The two-route auth split is
coherent: hard-auth `/verify` for the logout signal, soft-auth `/status` for the
poll.)

### M4 — semantic→hybrid on mount ✅ CORRECT
Live `SearchPage.tsx`: MODES includes `"semantic"` (:36); `mode` is seeded directly
from the URL at **:43-44** (`params.get("mode")`) with no readiness check; the query
effect at **:71-82** sends `&mode=${mode}` per keystroke (:77); "No results" at :101.
Live `search.py`: the two semantic calls at **:81/:86** are bare (no try/except),
unlike every other branch (:37-48,:50-64,:69-78,:94-103). v2 correctly (a) wraps
the bare calls server-side (§3.5) so hybrid degrades to keyword, and (b) makes the
client force `semantic→hybrid` **on mount** (because the URL can seed
`mode=semantic`) a HARD dependency of Phase 7, disabling the semantic button until
embeddings are `ready`. The §3.5 scope note is honest that the server fix alone
leaves pure-semantic returning `[]`, so the on-mount force is required, not
optional. Anchored to the exact seed site. **Resolved.**

**All six round-3 fixes: verified correct against live code.**

---

## PART 2 — Final adversarial sweep

### New bugs introduced by v2 edits — NONE found
- The `api()` try/catch (§4.3) re-throws unchanged and keeps `if (res.status===401)
  throw new ApiError(...,401)` (live :45) intact → `App.tsx:106` logout invariant
  untouched. ✅
- `connect()` calling `health.refreshNow()` (§4.2) fires an authed poll, but the key
  is already set (`setAccessKey(key)` at `App.tsx:79`, before the `/verify` at :81),
  so `authHeaders()` carries the bearer. No pre-auth race. ✅
- The folded `/verify` capability call runs inside the existing hard-auth handler
  (`auth_router.py:22-38`); `capabilities()` imports are lazy and cheap. No new
  import cycle (system_status imports services it doesn't depend back into). ✅
- `streamSSE` LLM-ok/llm-fail wiring (R3-M5) reads `{type:"error"}` (:797) /
  `{type:"done"}` (:795) which exist on the wire. ✅

### Integration coherence end-to-end ✅
boot/`connect()` → `ingestVerify(caps)` → `refreshNow()`/poll `/status` →
`ingestVerify` + rule 0 → store slices → `useHealth` → dot (§4.6 worst-of) + gates
(§4.7) + toast (§4.8), with observed-feed overlays from `api()`/`streamChat`/
`streamSSE` that can only **downgrade**. One predicate (`has_credentials()`) flows
from server `llm.state` to dot to every gate to the share landing. No contradictory
surfaces remain. Reachability is 3-axis (`navigator.onLine` + neterr/stall+no-byte
conjunction), replacing the `onLine`-only banner.

### Goal check ✅ — real-time server AND API health + warn-before-use everywhere
- **Server health:** 3-axis reachability incl. pre-auth on KeyEntry (skeleton path),
  ≤8s red on a dead VM. ✅
- **API/LLM health:** `has_credentials()` presence + observed-outcome `degraded`
  (now from BOTH chat and rebuild SSE, R3-M5), zero token burn. The H2 fix removes
  the "green dot, dead assistant" failure that previously undercut this claim. ✅
- **Warn-before-use for every unrunnable service incl. the public share route:**
  the §4.7 inventory covers embeddings/transcription/llm/push/geocoder; the public
  share route gates server-side via `llm_ready` on both landings (verified
  `_resolve_guided:192` already 404s on `not llm_ready()`). SharePage uses the
  separate `publicApi` (api.ts:131) and is carved out of the poller — the recipient
  is warned by the server-driven landing flag, not by owner instrumentation. ✅

### Remaining HIGH/MEDIUM re-scrutiny — none open
- **Offline-auth invariant (`App.tsx:106`):** exact match
  `if (e?.status===401) clearAccessKey(); else setAuthed(true);`. Poller/getStatus
  never call `clearAccessKey`; observed feed re-throws. Safe by construction. ✅
- **Cross-origin:** `main.py:232-242` CORS `allow_origins` default `["*"]`,
  `allow_credentials` not set (off), bearer-only via private `authHeaders()`. Status
  path carries no cookies. ✅
- **Public-skeleton security:** two independent builders; the public path returns
  exactly `{ok,brain,ts}` (allowlist-tested, §8) — leaks no more than
  `/api/health`+`/auth/info`. `last_error[:200]` is authed-only. ✅
- **Re-render/toast storms:** `useSyncExternalStore` singleton (slice subscriptions);
  SearchPage `:79` kept excluded from toasts; silent-catch promotion only when
  `server==="ok"`; M2 fix removes the user-abort false-stall toast. ✅
- **Multi-worker:** single worker (`Dockerfile:45`, no `--workers`); LOUD comments
  in both readiness modules. ✅
- **Cost/tokens:** zero — `verified:null`, no synthetic probes, no model load per
  poll, ≤1 `SELECT 1`. ✅
- **Lock discipline:** single `threading.Lock` around `_set_state` + snapshot read;
  `_set_state` inside `_get_model` (runs on `to_thread` worker); `readiness()` does
  NOT take `_model_lock`. Audio keys off `_model_key == want`, set only after success
  → failed reload reads `warming`, never false `ready`. ✅

### Scope / over-build (single-user self-hosted) ✅
Disciplined. Pydantic `CapState`/`Capability` models deferred (§3.9, no OpenAPI
consumer); scheduler heartbeat cut; SSE/WebSocket push out of scope; multi-worker
sharing out of scope. `needs-auth` is kept but now actually implemented and cheap
(one boolean check). Nothing over-built.

---

## Minor / OPTIONAL polish (NON-BLOCKING — do not gate presentation)

1. **Anchor pedantry (cosmetic):** the plan repeatedly cites the boot snapshot as
   `App.tsx:102`; the `get("/api/auth/verify")` call is on **:101** and `.then` on
   :102. Resolves correctly; tighten to ":101-102" if convenient.
2. **`refreshNow()` on `connect()` is technically redundant with the immediate
   `ingestVerify(v)` seed** (both happen at login). Harmless (the poll also sets
   reachability/`lastOkAt` and starts cadence), but an implementer could note the
   `ingestVerify` gives instant caps and `refreshNow` exists mainly to kick the
   cadence/reachability — keep both, as the plan says.
3. **`db.last_error` typing:** the TS `Capabilities.db` has `last_error?: string|null`
   (optional) while embeddings/transcription have it required; minor asymmetry,
   matches the server shape (db omits it on success). Fine; just intentional.
4. **§4.7 row "Search hybrid … embeddings preferred":** ensure the "(keyword only —
   semantic loading)" note keys off the same `embeddings.state!=="ready"` the
   on-mount force uses, so hybrid and the semantic-button-disable stay in sync. The
   plan implies this; an explicit shared selector would prevent drift. Optional.

None of these affect correctness, security, or the owner goal.

---

## Bottom line

HYBRID v2 fixes all six round-3 blockers correctly (re-verified against
`clock.py`, `llm.py`, `config.py`, `share.py`, `auth.py`, `search.py`, `App.tsx`,
`api.ts`, `SearchPage.tsx`, `main.py`, `auth_router.py`, `Dockerfile`), introduces
no new bug, regresses none of the carried invariants, and leaves no open
HIGH/MEDIUM. It is end-to-end coherent and delivers the owner's two asks including
the public share route. **GO — this is ready to be the final/presented plan.**
