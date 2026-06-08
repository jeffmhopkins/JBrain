# Calendar UI Redesign — Hybrid Plan

Status: **PROPOSAL (awaiting approval).** Produced by a best-of-N gauntlet: three
independent design plans (native-calendar / calm-minimalist / power-desktop-a11y)
→ an advisor reconciliation → this single hybrid. Mockup: `calendar-mockup.png`
(rendered from `mockup.py` + `render.py`, styled with the live `styles.css` tokens).

## Goal
Replace the agenda-only Calendar page with **List view** + a **real calendar** you
can switch across **Day / Week / Month**, with ‹ Today › navigation — staying calm,
mobile-first, and faithful to "notes are the source of truth".

## Settled architecture (all three agreed)
- Calendar mounts as a full-screen **Advanced tool** (`.tool`/`.tool-bar`/`.tool-body`);
  add `TOOL_TITLES["/calendar"] = "Calendar"`; drop the page's own `<h2>`/intro.
- Reuse the existing `.seg` segmented control and `.modal` / `.modal-compact` sheets.
- New read endpoint **`GET /api/calendar/range?from=&to=`** with **server-side**
  recurrence expansion (reuse `calendar.expand_rrule`). List view keeps
  `calUpcoming` / `calHistory`.
- State = `viewMode` + `cursor` → derived `[from,to]`; range cache; stale-response
  guard keyed on the range; `localStorage` view persistence.
- The calendar's only edit actions are Add (quick-add, which writes a note) and Remove
  (a note-free dismissal, reversible via an Undo snackbar). Reschedule/cancel are done by
  editing the note itself. *(An earlier revision exposed reschedule/cancel controls here;
  they were later removed in favor of editing notes directly.)*

## Adjudicated decisions (the hybrid)
1. **Day = time-axis grid** (hour rows, all-day lane, "now" line, overlap columns).
   **Week = stacked day-sections** (reuses the List row renderer — calm + legible on a
   phone; a 7-column pixel grid is illegible at ~50px/col). A desktop 7-col time-grid
   is a deferred enhancement.
2. **Default view = List** on phone (always); desktop honors last-used, falling back to
   List. First run = List everywhere. (Month-first is a desktop instinct that gives a
   phone user dots instead of their schedule.)
3. **Month day-tap → an agenda strip directly below the grid** (one component on every
   breakpoint; grid stays in context). A header affordance ("Open day →") jumps to Day.
4. **Kinds = monochrome** glyph + weight + a dim text label (`appt` / `deadline` /
   `reminder`); the lone `--accent` is reserved for today/selection. Per-kind colors are
   rejected — they'd collide with the app's `--m-*` *mode* palette and break the
   one-accent discipline.
5. **Desktop two-pane (mini-month + rail) is deferred** — the Month+agenda-strip already
   delivers most of its value in one responsive component; build the single column well first.
6. **Phone Week = stacked day-sections**, swipe/prev-next to move weeks. (The day-strip
   collapse is a v2 nicety.)

## Shared risks to handle in implementation
- **`/range` endpoint:** validate ISO + `from<=to` (422, mirror `_compose_starts`),
  **clamp the span** (e.g. ≤ 366d) so recurrence expansion can't blow up, and reuse the
  existing supersession + `status NOT IN ('cancelled','done')` filtering (the grid may
  show cancelled as struck-through, but never resurrect superseded rows).
- **Recurring true times:** the current `upcoming` path forces recurring occurrences to
  `all_day:1`; `/range` must preserve each occurrence's real time so the time-grid places
  them correctly (`expand_rrule` already supports timed starts).
- **Timezone:** storage is UTC, one owner-local zone via `clock.py`. Server returns
  owner-local ISO; client treats strings as wall-clock and never re-parses to UTC —
  otherwise the now-line and midnight bucketing drift.
- **`ends_at` often null** → fixed default block height (60 min); `ends_at < starts_at`
  → clamp; cross-midnight → clip at the day boundary in v1.
- **Multi-day all-day** has no model support → render as a single chip on the start day
  in v1 (document the limit; no spanning bars yet).
- **Cache invalidation:** a reschedule moves an event across windows → **fully clear**
  the range cache after any mutation, not just the current window.

## Phased build order
**v1 (first PR — independently shippable):**
1. Mount Calendar as an Advanced tool (shell migration; no behavior change).
2. `GET /api/calendar/range` (+ `calRange` in api.ts; widen `CalEvent` so `ends_at`/
   `location_label` are reliably present) with clamp/validation/filtering + tests.
3. List view inside the tool shell + the `.seg` view switcher.
4. State core (viewMode/cursor → range; cache w/ full-clear-on-mutate; stale guard;
   prev/next/Today; localStorage).
5. **Month** view (monochrome grid; tap → agenda strip) — proves the range path.
6. Monochrome kind treatment across List/Month.
7. Detail sheet → `.modal-compact` with reminder chips + a single "Remove from calendar"
   action (note-free dismissal; Undo snackbar). *(Originally a reschedule/cancel form;
   simplified to Remove-only — reschedule/cancel are done by editing the note.)*

**v1.1 (fast follow):** 8. **Day** time-axis grid (now-line, overlap columns, scroll-to-now).
9. **Week** stacked day-sections (reuses the List renderer) + week swipe.

**Deferred (v2+):** desktop two-pane; swipeable day-strip Week + bottom-sheet day detail;
multi-day all-day spanning bars; per-occurrence recurring edits (needs EXDATE model work).

## Files (when approved)
- `web/src/pages/CalendarPage.tsx` (rewrite → container + views + sheets)
- `web/src/api.ts` (`calRange`, widen `CalEvent`)
- `web/src/components/Shell.tsx` (`TOOL_TITLES["/calendar"]`)
- `web/src/styles.css` (calendar grid / time-axis / now-line classes; reuse `.seg`/`.modal`)
- `server/app/routers/calendar.py` (`GET /api/calendar/range`, reuse supersession filter + `expand_rrule`)
