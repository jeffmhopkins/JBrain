// A card the user can drag horizontally. A guarded RIGHT-swipe hides it from view (live
// finger-tracking: the body follows the finger over a "Hide" panel, then slides off and
// collapses the gap it leaves — view-only, the note's DB row is untouched). A guarded
// LEFT-swipe fires `onSwipeLeft` (used to open an assistant reply's tool-call history).
//
// The two thresholds live in `swipeGesture.ts` and are unit-tested there; this component is
// the live wiring (transform during the drag, spring-back below threshold, animated dismissal).
import { useRef, useState, type ReactNode, type TouchEvent } from "react";
import { Icon } from "./Icon";
import { shouldHideOnSwipe, shouldOpenHistoryOnSwipe } from "../swipeGesture";

const SLIDE_MS = 190;   // body slides off the right edge
const COLLAPSE_MS = 160;   // …then the empty row collapses, then `onHide` fires

interface SwipeCardProps {
  /** Remove this card from the view (the right-swipe dismissal has finished animating). */
  onHide: () => void;
  /** Optional left-swipe action (assistant replies pass their open-history toggle here). */
  onSwipeLeft?: () => void;
  children: ReactNode;
}

/** Render a horizontally swipeable chat card (right → hide, left → `onSwipeLeft`). */
export default function SwipeCard({ onHide, onSwipeLeft, children }: SwipeCardProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  const horizontal = useRef(false);   // gesture has committed to horizontal (vs a vertical scroll)
  const [revealed, setRevealed] = useState(false);   // show the "Hide" panel behind the body

  function moveBody(px: number, animate: boolean) {
    const el = bodyRef.current;
    if (!el) return;
    el.style.transition = animate ? `transform ${SLIDE_MS}ms ease, opacity ${SLIDE_MS}ms ease` : "none";
    el.style.transform = px ? `translateX(${px}px)` : "";
    el.style.opacity = "1";
  }

  function onTouchStart(e: TouchEvent<HTMLDivElement>) {
    const t = e.touches[0];
    start.current = { x: t.clientX, y: t.clientY };
    horizontal.current = false;
  }

  function onTouchMove(e: TouchEvent<HTMLDivElement>) {
    const s = start.current;
    if (!s) return;
    const t = e.touches[0];
    const dx = t.clientX - s.x;
    const dy = t.clientY - s.y;
    if (!horizontal.current && Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) horizontal.current = true;
    // Only the rightward (hide) drag tracks the finger; a leftward (open-history) swipe has no
    // drag visual, matching how that gesture has always behaved.
    if (horizontal.current && dx > 0) {
      if (!revealed) setRevealed(true);
      moveBody(dx, false);
    }
  }

  function dismiss() {
    const body = bodyRef.current;
    const wrap = wrapRef.current;
    if (body) {
      body.style.transition = `transform ${SLIDE_MS}ms ease, opacity ${SLIDE_MS}ms ease`;
      body.style.transform = "translateX(110%)";
      body.style.opacity = "0";
    }
    // After the body has slid away, collapse the row's height so the thread closes the gap,
    // then drop the card from the view. Timers (not transitionend) so it works headless too.
    window.setTimeout(() => {
      if (wrap) {
        wrap.style.maxHeight = `${wrap.offsetHeight}px`;
        wrap.style.overflow = "hidden";
        requestAnimationFrame(() => {
          wrap.style.transition = `max-height ${COLLAPSE_MS}ms ease, margin ${COLLAPSE_MS}ms ease, opacity ${COLLAPSE_MS}ms ease`;
          wrap.style.maxHeight = "0";
          wrap.style.marginTop = "-12px";   // absorb the parent's row gap
          wrap.style.opacity = "0";
        });
      }
      window.setTimeout(onHide, COLLAPSE_MS);
    }, SLIDE_MS);
  }

  function onTouchEnd(e: TouchEvent<HTMLDivElement>) {
    const s = start.current;
    start.current = null;
    if (!s) return;
    const t = e.changedTouches[0];
    const end = { x: t.clientX, y: t.clientY };
    const selLen = window.getSelection?.()?.toString().length ?? 0;
    if (shouldHideOnSwipe(s, end, selLen)) {
      dismiss();
      return;
    }
    if (onSwipeLeft && shouldOpenHistoryOnSwipe(s, end, window.innerWidth, selLen)) onSwipeLeft();
    setRevealed(false);
    moveBody(0, true);   // spring back
  }

  return (
    <div className="swipe-wrap" ref={wrapRef}>
      {revealed && (
        <div className="swipe-reveal" aria-hidden="true">
          <Icon name="eyeOff" size={15} /> Hide
        </div>
      )}
      <div className="swipe-body" ref={bodyRef}
           onTouchStart={onTouchStart} onTouchMove={onTouchMove} onTouchEnd={onTouchEnd}>
        {children}
      </div>
    </div>
  );
}
