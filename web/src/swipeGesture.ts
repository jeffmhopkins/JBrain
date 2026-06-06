// The guarded left-swipe that opens a reply's tool-call history. Pure + testable because the
// thresholds are the whole point: a too-loose gesture re-introduces the bug that got horizontal
// swipes removed app-wide (collision with the OS back/forward edge gesture), and fights both
// vertical scrolling and text selection. Opening history is only worth it when the swipe is
// unambiguous.
export const SWIPE_MIN_DX = 55;       // px of leftward travel required
export const EDGE_GUARD = 28;         // px from the right screen edge reserved for the OS gesture

export function shouldOpenHistoryOnSwipe(
  start: { x: number; y: number },
  end: { x: number; y: number },
  innerWidth: number,
  selectionLength: number,
): boolean {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  if (dx > -SWIPE_MIN_DX) return false;                 // must be a clear LEFTward drag
  if (Math.abs(dx) < Math.abs(dy) * 1.5) return false;  // …horizontal-dominant, not a scroll
  if (start.x > innerWidth - EDGE_GUARD) return false;  // …not starting in the OS edge-gesture zone
  if (selectionLength > 0) return false;                // …not while selecting text to copy
  return true;
}
