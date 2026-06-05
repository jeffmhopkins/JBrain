// Cross-component nav signal. A REPLACE navigation (react-router `{replace:true}`) swaps
// the current route rather than adding history — so the back-stack in Shell must SWAP its
// top, not push a new level (otherwise back lands on the replaced-away route, e.g. a note's
// pre-rename slug → 404). A component sets this immediately before such a navigate; Shell's
// stack effect reads-and-clears it. Kept as a module flag (not context) so it needs no
// provider and can't desync across renders — it's a one-shot handoff consumed on the very
// next location change.
let _replaceNext = false;

export function markReplaceNav(): void {
  _replaceNext = true;
}

export function takeReplaceNav(): boolean {
  const v = _replaceNext;
  _replaceNext = false;
  return v;
}
