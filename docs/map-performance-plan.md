# Map Performance Plan — Large Point Sets (gauntlet-hardened v3)

**Goal:** keep the location map (`web/src/pages/MapPage.tsx`) responsive over large trails
(12k–20k points, multi-person), targeting the user's actual symptom — **lag while zooming/panning
with lots of points** — plus playback smoothness. Levers: **zoom-based compression** (decimate when
zoomed with many points) and, only if warranted, **viewport truncation**.

App context (drives proportionality): self-hosted personal/family "second brain", a few tracked
people, realistically low-tens-of-thousands of points — **not** a public service with millions.

> **v2 changelog (after adversarial gauntlet review).** Phase 3 (backend bbox/server-DP/indexing)
> **deleted** as YAGNI at this scale. Added **Phase 0 — Measure** (profile before optimizing).
> Re-anchored the diagnosis to pan/zoom (the stated symptom), correcting that `setLatLngs` avoids JS
> churn but **not** Leaflet's full-canvas repaint. Demoted the effect-split (1B) to measure-gated.
> Fixed three High-severity correctness bugs the gauntlet found: (a) the binary-search "array is
> chronological" assumption is false; (b) slicing a DP-simplified array by a raw index is an
> index-aliasing bug; (c) neighbor-based culling drops viewport-spanning segments. Added the LIVE-poll
> O(N) memo-churn fix. Replaced the unsupported "5–50×" claim with measured ratios.
>
> **v3 (4th reviewer — feasibility/sequencing).** Made 1B self-contained via a required vertex cap so
> close-up smoothness no longer secretly depends on Phase 2 (was an internal contradiction). Rewrote
> Verification: seeding **must** use direct DB insert (the ingest dedup rule silently rejects dense
> synthetic clusters); added the `npm run build` typecheck gate (no web test runner exists); invariant
> checks are manual-with-instrumentation. Added the 2A ref/stale-closure contract (prevents
> leaks/double-adds) and per-sub-item commit granularity (2A must be atomic).

Scope: `web/src/pages/MapPage.tsx`, `web/src/api.ts` (+ a one-line `locations.py` cap fix).

---

## Corrected root-cause analysis

The original plan named "per-tick O(N) overlay rebuild during scrub/playback" as primary. The gauntlet
showed that's only half right and aimed at the wrong interaction:

- **Pan runs no overlay JS at all** — the only map handlers are `click` (`MapPage.tsx:181`) and
  `zoomend` (`:209`). There is no `move`/`moveend` handler touching the overlay. Pan lag is purely
  **Leaflet's canvas re-raster** of the polyline.
- **Zoom runs the overlay effect exactly once** (on `zoomend` → `zoomLevel` dep), not per tick. The
  lag *during* a zoom gesture is again Leaflet re-projecting/re-rastering, not the JS rebuild.
- **`epsilonForZoom` returns 0 at z≥17** (`:64`) → the **raw full track** is handed to one
  `L.polyline`. Even though only a few on-screen vertices are *drawn*, Leaflet still iterates/clips
  **all N** in that single polyline on every redraw. This is the concrete "zoom in on a dense area →
  lag" case.
- **Playback** (`curTs` every 120 ms, `:416`) *does* trigger the full O(N) teardown+reparse+DP
  rebuild (`:355-411`) — a real but separate problem (playback smoothness, not pan/zoom).
- **LIVE poll churn:** every 15 s `setPoints(prev => [...prev, ...add])` (`:241`) creates a new array
  ref, invalidating `presentPeople` (`:147`), `timeline` (`:250`), and `heat` (`:345`) — each a full
  O(N) recompute with N (or 2N) string-date parses, **even when idle**. Arguably the most constant
  user-visible hitch.

**What actually helps the user's complaint:** reducing the **drawn/iterated vertex count** (fix the
z≥17 cliff; cap vertices) so Leaflet's raster is cheap. **What helps playback:** parse-once + don't
re-walk N per tick. Note: `setLatLngs` on a canvas polyline avoids JS object churn but Leaflet's
`L.Canvas` still **clears and repaints the whole layer** on any geometry change — so an effect split
helps playback JS, not raster.

---

## Phase 0 — Measure (mandatory, before any code)

Without this we risk optimizing the wrong path.
- Seed a realistic dataset (see Verification for the dedup-aware method) and profile the **three real
  interactions** with the browser Performance panel, separating **Scripting** (JS) from
  **Rendering/Painting** (Leaflet raster): (1) pinch/scroll zoom on a dense cluster, (2) pan at z16,
  (3) playback at "All" range.
- Record baselines: frame time per interaction; memo recompute time on a poll tick.
- **Gate:** each later phase ships only if it moves its target metric vs. this baseline. Replace any
  "Nx" payload claims with the measured DP reduction ratio on the seeded data.

---

## Phase 1 — Frontend, minimal & high-leverage (no new deps, no API change)

Ordered so the two lowest-risk, highest-leverage items land first.

### 1A. Parse timestamps once  *(must — trivial, pure win)*
- Add `pointTs`/`noteTs` numeric memos. **Sort `pointTs` together with a parallel index map** rather
  than assuming load order — the live poller appends out of order and `/bulk` backfills insert older
  timestamps (gauntlet High #1). Keep `points`/`pointTs` index-aligned (don't reorder one without the
  other), since the `heat` dwell memo indexes `points[i+1]` (`:348`).
- Replace **all** `parseTs` call sites that run on `curTs`/poll: `:252,:253` (timeline), `:335` (note
  reconcile — use `noteTs`), `:348` (heat dwell — numeric gap), `:361`, `:396`, `:405`.
- **Acceptance:** zero `parseTs` inside any `curTs`-keyed effect/memo.

### 1B. Memoize DP per (data, zoom, filter); fix the z≥17 cliff  *(must — the user's complaint)*
- Wrap each person's simplified track in a `useMemo` keyed `[points, zoomLevel, personOf, hidden]` so
  DP runs once per settled zoom, not per tick.
- **Store retained-vertex timestamps alongside the simplified coords** — `{ coords: [lat,lon][], ts:
  number[] }` (gauntlet High #2). DP discards the `keep` map today (`:57-59`); we must keep the
  per-kept-vertex original timestamp so the cursor can find the correct slice boundary in the
  *simplified* array. Without this, slicing the simplified array by a raw index is an aliasing bug.
- Change `epsilonForZoom` (`:64`): at z≥17 use a **tiny non-zero epsilon** instead of `0`, so even a
  close-up dense track is lightly thinned and Leaflet iterates far fewer vertices.
- **Required in-phase guarantee (not optional):** cap drawn vertices per person to a fixed budget
  (e.g. ~3k) via stride when a simplified track still exceeds it. This is what makes 1B
  **self-contained** — it bounds raster cost at any zoom **without** depending on viewport culling
  (which is the deferred 2C). The original v1 wording leaned on Phase 2 culling for z18 smoothness,
  an internal contradiction (gauntlet #4); the vertex cap removes that dependency.
- **Acceptance (honest, Phase-1-boundary):** DP runs once per settled zoom (`console.count('DP')` in
  the memo body stays flat while scrubbing at fixed zoom); the per-tick re-walk of all N is gone; z18
  on a dense cluster draws ≤ the vertex cap. **Full** zero-culling close-up perfection on a 15k dense
  cluster is explicitly a 2C concern — 1B's gate is "bounded, no longer re-walked per tick", measured
  against the Phase 0 baseline.

### 1C. Kill LIVE-poll O(N) memo churn  *(should — constant idle hitch)*
- Cache per-point parsed timestamps by `id` (stable across the new-array-ref append) so `timeline`,
  `heat`, and `presentPeople` don't re-parse all N every 15 s; or maintain these derived arrays
  additively on append (new fixes are appended, so timeline/heat can extend rather than rebuild).
- **Acceptance:** a poll tick on a 15k trail does no full-N reparse (Phase 0 metric).

### 1D. "All"-range truncation one-liner  *(should — real data-loss bug, tiny fix)*
- `DESC+LIMIT+reverse` (`locations.py:186-191`) silently drops the **oldest** fixes and is global
  across people. Fix minimally: for the "All" range raise/remove the cap, **or** add a single
  `truncated: true`-style signal. **No** new headers, per-person budgets, or endpoints (cut by
  gauntlet as scope inflation).

### Phase 1 invariants (acceptance gates)
- Head dot on each person's true newest fix; line terminates at it. The cursor uses **two distinct
  searches**: raw `pointTs` for the head-dot's exact position, simplified `ts` for the line-tail slice.
- A person with **zero fixes ≤ curTs** renders no line/dot (preserve current "grow-in" behavior); the
  "no teardown" goal applies to geometry, with per-person appearance toggles allowed (gauntlet Med #2).
- Per-person colours and `presentPeople` chips unchanged; never merge sources.
- Heat dwell uses the **full** sequence; heat slicing uses the **raw** cut index (full-res,
  index-aligned), explicitly distinct from the trail's simplified-array cut.
- Timeline = full union of all fix-times + note-times.

---

## Phase 2 — Optional, measure-gated

### 2A. Effect split (geometry vs. cursor)  *(spike only if playback still janks after 1A/1B)*
- Split `:355-411` into a geometry effect (`[points,mode,personOf,hidden,heat,heatLevel,zoomLevel]`)
  and a `[curTs]`-only cursor effect that updates `setLatLngs`/`setLatLng` from the cached simplified
  geometry. Benefit is **JS churn only** — Leaflet still repaints the whole canvas layer on geometry
  change, so this helps playback, not pan/zoom. Carries the most refactor risk (stale closures, ref
  lifecycle, double-adds) → gate on a Phase 0 playback measurement, don't front-load.
- **Required ref contract (gauntlet #3 — prevents leaks/double-adds):**
  (a) one ref holding `Map<personId, { line, head, heatHandle }>`;
  (b) the geometry effect **removes and clears every handle in that map at the top of each run** before
  rebuilding (preserving today's teardown safety at `:357-358`), and its cleanup does the same — so a
  trail↔heat `mode` switch can't leak the prior mode's layer, and a person moved into `hidden` has its
  handle removed **and deleted** from the map;
  (c) the cursor effect reads `points`/`pointTs`/`hidden`/`mode` via **refs** (extend the existing
  `pointsRef` pattern at `:87,154`) so its only reactive dep is `curTs` and it never closes over a
  stale `pointTs` after a live-poll append;
  (d) live append changes `points`, so the geometry effect's `[points]` dep covers the rebuild, and the
  ref reads must refresh in that same render.
- **Atomicity:** 2A must land as a **single commit** — both effects plus every migrated `parseTs` call
  site together. A partial split (e.g. trail migrated, heat mode still re-parsing at `:396`) leaves the
  component in an inconsistent half-migrated state.

### 2B. Heat-input binning  *(do if heat mode is used on large ranges)*
- Bin the heat array into a fixed-decimal grid by zoom, summing dwell per cell — collapses ~20k
  stacked points to a handful of weighted cells. **Reconcile with `curTs`:** either precompute
  cumulative binned prefixes, or accept that scrubbing shows unbinned points and only the settled view
  is binned — state which (gauntlet Med #6).

### 2C. Viewport culling  *(deferred — DP after 1B already removes most off-screen cost)*
- If still needed: cull on debounced `moveend` using **segment–rectangle intersection** (Cohen–
  Sutherland / Liang–Barsky), **not** "keep-if-neighbor-inside" — the neighbor rule drops a long
  segment that spans the viewport with both endpoints outside (gauntlet High #5). Trail-mode only;
  heat exempt. Guardrails: full-range load still drives fit/timeline/dwell/chips (cull affects
  *rendering* only); head dot bypasses culling; deep links `?focus`/`?place` still work; debounce +
  abort stale fetches; no overlay teardown on pan.

---

## DELETED from v1 (gauntlet cut-list)
- **All of original Phase 3** — composite index + migration, bbox query params + antimeridian
  validation, **client region-cache**, a **second Python copy of Douglas–Peucker**, ETag/Cache-Control,
  `X-Truncated`/`X-Returned` headers, per-person budgets. Unjustified at family scale; a written spec
  becomes an obligation. Replacement policy: *if network transfer (not render) is ever measured as the
  bottleneck on real data, open a ticket then.* The one real bug in that phase (oldest-fix drop)
  survives as the 1D one-liner.

---

## Verification

- **Seeding — direct DB insert is REQUIRED (gauntlet #1).** Ingest enforces keep-only-if ≥30 m moved
  OR ≥60 min elapsed per source (`locations.py:25-26,79,144`) and `/bulk` caps 5000/request and
  truncates silently (`:114`, returns `{stored, received}`). A naive generator scattering tight dwell
  clusters has **nearly every point dropped** — the exact dense-cluster dataset Phase 1B/2B need to
  stress **cannot** be produced via `/bulk`. Demo mode returns `[]` for locations (`web/src/demo.ts`),
  so it's not a seed source either. **Use a direct `INSERT INTO locations (lat, lon, accuracy_m,
  recorded_at, source, person_id) VALUES (...)` loop** against the dev SQLite (mirror the column list
  at `locations.py:147-150`); set `person_id`/`source` for ≥2 people so per-person colouring and
  `presentPeople` are exercised. (Only if testing the *ingest* path itself: generate a genuinely moving
  track ≥40 m between consecutive same-source fixes, chronological chunks ≤5000, assert
  `stored == received`.)
- **Run the app (from README:232-236):** `cd server && uvicorn app.main:app --reload`, then
  `cd web && npm install && npm run dev`.
- **Build / typecheck gate (gauntlet #6):** there is **no** `test`/`lint`/`typecheck` script in
  `web/package.json` (only `dev`/`build`/`preview`) and **no web test runner**. So after each web PR
  run `cd web && npm run build` (which runs `tsc -b && vite build` — the de-facto typecheck) and
  confirm zero errors before manual testing. The new generic refs (`Map<personId, L.Polyline>`) and
  `setLatLngs`/`setLatLng` calls make a type error a live risk.
- **Invariant checks are MANUAL with instrumentation (gauntlet #2)** — no automated harness exists.
  Per invariant, add a temporary probe: 1A → grep the diff for `parseTs` inside `curTs`-keyed
  effects/memos; 1B → `console.count('DP')` in the memo, scrub at fixed zoom, confirm flat; 2A →
  `console.count('removeLayer')`, confirm it does not advance while playing. (Standing up
  vitest + @testing-library + a Leaflet mock is a separate, out-of-scope task.)
- **Manual gates:** Phase 0 profiles re-run after each phase (smooth zoom on dense cluster; pan at z16
  no flicker; playback at "All"); people chips; heat dwell hotspots unchanged vs. baseline; deep-link
  `?focus`/`?place`; live-poll append moves head dot.
- **Commit granularity (gauntlet #5):** **1A, 1B, 1C, 1D each their own commit/PR** (pure, individually
  revertable). **2A must be a single atomic commit** — it cannot be split across PRs. Run the build
  gate + manual probes before each merge so the hot component is never left half-migrated.

## Open questions
- Confirm Leaflet `L.Canvas` repaint behavior empirically if 2A is pursued (bounds its payoff).
- Tune z≥17 epsilon and the vertex cap against the Phase 0 dataset.

---

# Iteration 2 — the REAL bottleneck was the note pins (post-Phase-1 follow-up)

Phase 1 shipped but the map still stuttered on **large date ranges**. A second multi-agent
diagnosis + a user device test pinned the actual cause, which Phase 1 never touched.

## Evidence (decisive)
- Symptom: **stutter *while dragging*** on **phone/tablet**, in **BOTH Trail and Heatmap**,
  worse with large date ranges; **zooming in to a few points → smooth**; **toggling Notes
  off → smooth**.
- "Both modes" rules out the trail polyline (Trail) and the heat canvas (Heatmap) — they are
  different render paths. The only heavy thing common to both, scaling with date range, is the
  **note pins**.
- Note pins are `L.marker` with **`divIcon` (a real DOM node + emoji per note)**
  (`MapPage.tsx` ~`noteIcon`/the build-markers effect). A large range loads many located
  notes → many DOM nodes. On a mobile/retina device the browser paints/composites the on-screen
  ones every drag frame → stutter. Off-screen DOM nodes are paint-culled, so zooming in (few
  on-screen) is smooth. Confirmed by the user's "Notes off = smooth" test.
- Measurement (`/tmp` seed of 18k–30k pts) confirmed the trail is NOT the issue at the fitted
  zoom: DP already reduces it to ~1.8k–3k vertices and `VERTEX_CAP=3000` is inert there.

## Fix — render note pins on the shared canvas instead of DOM
Replace the per-note `L.marker(divIcon)` with **`L.circleMarker`** on the existing
`preferCanvas` renderer:
- **Zero DOM nodes per note** — all pins draw into the one shared `<canvas>`, which during a
  drag is GPU-transformed (not repainted). This is exactly what made the trail smooth.
- Leaflet's canvas renderer **auto-skips off-screen** circleMarkers (per-layer pxBounds
  intersect check), so cost is bounded to on-screen pins and only on `moveend`, not per drag
  frame.
- Keep the existing layerGroup + scrub-time reconcile + `bindPopup`/`openPopup` (circleMarker
  supports the full Layer/Popup API), so click→note, the "Save as place" popup, and the
  `?focus=<slug>` deep-link `openPopup()` all keep working unchanged.
- Style the dot **distinct from the person head dots** (small opaque white-ringed circles):
  a larger translucent magenta ring (`radius 8, weight 3, #e91e63, fillOpacity 0.22`) —
  differentiated by shape+size+hue, not colour alone (survives colour-blindness; per review).
  Radius 8 keeps a usable mobile tap target (~9.5px), backed by the existing 200 m "notes
  here" map-tap fallback. **No separate renderer** (that would create a second canvas and
  forfeit the shared-canvas drag win). Leave the trail vertex budget UNTOUCHED — measurement
  says it isn't the bottleneck, so changing it is scope creep / fidelity risk.

### Why not the alternatives
- **Viewport-culling the DOM markers** (add/remove on moveend): no new dep, but keeps DOM
  nodes (still many when zoomed out), adds moveend churn, and Leaflet already paint-culls
  off-screen — so it under-delivers vs. moving to canvas.
- **markercluster / supercluster**: a new dependency, cluster icons are still DOM, and it
  changes UX (pins merge into count bubbles). Overkill for the stated problem. Revisit only if
  note counts reach the thousands AND canvas markers prove insufficient.

### Acceptance
- Dragging a large-range view with Notes ON is smooth on mobile (the reported case), with no
  objectionable hitch on drag-release at the largest range (the new shared-canvas moveend cost).
- Click a pin → popup; "Save as place" works; `?focus=<slug>` opens the pin's popup.
- Scrub still reveals pins by time; Notes toggle still works; no DOM node per note (verify in
  devtools elements panel — the marker pane stays empty).
- `npm run build` clean.
