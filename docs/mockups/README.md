# Interactive UI mockups

Self-contained HTML mockups used to agree on **how a GUI change looks and feels
before it's built**. Each is a single file with no build step or dependencies —
open it straight in a browser (or double-click), or serve the folder.

These are throwaway design aids, not shipped code. They mirror the real app's
theme tokens and the relevant logic (e.g. swipe thresholds) so the feel is
honest, but they don't import from `web/` and aren't covered by tests.

## The requirement they exist for

**Visual/interaction changes get an interactive mockup, with options, reviewed
*before* implementation.** A static screenshot or an ASCII sketch is not enough
for a gesture/animation/layout decision — the reviewer needs to *feel* it. See
the "GUI / UX changes" section in the repo root `CLAUDE.md` for the full rule.

## Mockups

| File | Change it explores |
| --- | --- |
| [`swipe-right-hide.html`](./swipe-right-hide.html) | Swipe-right to hide an entry card from the chat view (view-only, no DB write). Compares four styles: **A** instant hide, **B** slide-out + fade, **C** live drag + reveal, and **D** an optional "Undo" toast that layers on any of them. Drag a card to the right; **Reset** restores them. **Shipped: option C** (live finger-tracking over a "Hide" panel, then slide-off + collapse) — see `web/src/components/SwipeCard.tsx`. |
