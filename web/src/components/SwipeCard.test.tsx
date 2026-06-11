// Unit/integration tests for SwipeCard: the live horizontal-swipe wiring. The threshold maths
// itself is covered in swipeGesture.test.ts; here we assert the component reacts to a touch drag
// — right past threshold → onHide (after the dismiss animation), left → onSwipeLeft, a short drag
// → neither (spring back), and that the "Hide" panel reveals while dragging right.
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, fireEvent, cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import SwipeCard from "./SwipeCard";

afterEach(cleanup);

// jsdom touch events don't carry coordinates by default; supply them explicitly.
const at = (x: number, y = 100) => ({ clientX: x, clientY: y });
function swipe(el: Element, fromX: number, toX: number, { withMove = true } = {}) {
  fireEvent.touchStart(el, { touches: [at(fromX)] });
  if (withMove) fireEvent.touchMove(el, { touches: [at(toX)] });
  fireEvent.touchEnd(el, { changedTouches: [at(toX)] });
}

function setup(props: Partial<React.ComponentProps<typeof SwipeCard>> = {}) {
  const onHide = vi.fn();
  const onSwipeLeft = vi.fn();
  render(
    <SwipeCard onHide={onHide} onSwipeLeft={onSwipeLeft} {...props}>
      <div className="msg user">Buy milk</div>
    </SwipeCard>,
  );
  return { onHide, onSwipeLeft, card: screen.getByText("Buy milk") };
}

describe("SwipeCard", () => {
  it("hides on a clear right-swipe (after the dismiss animation)", async () => {
    const { onHide, onSwipeLeft, card } = setup();
    swipe(card, 100, 230);   // dx = +130, well past the 55px threshold
    await waitFor(() => expect(onHide).toHaveBeenCalledTimes(1));
    expect(onSwipeLeft).not.toHaveBeenCalled();
  });

  it("reveals the “Hide” panel while dragging right", () => {
    const { card } = setup();
    expect(screen.queryByText("Hide")).not.toBeInTheDocument();
    fireEvent.touchStart(card, { touches: [at(100)] });
    fireEvent.touchMove(card, { touches: [at(170)] });
    expect(screen.getByText("Hide")).toBeInTheDocument();
  });

  it("fires onSwipeLeft on a clear left-swipe and does not hide", async () => {
    const { onHide, onSwipeLeft, card } = setup();
    swipe(card, 230, 100);   // dx = -130 leftward
    expect(onSwipeLeft).toHaveBeenCalledTimes(1);
    // Give the (non-existent) dismiss timers a chance — onHide must stay untouched.
    await new Promise((r) => setTimeout(r, 400));
    expect(onHide).not.toHaveBeenCalled();
  });

  it("springs back (no callbacks) when the drag is too short", async () => {
    const { onHide, onSwipeLeft, card } = setup();
    swipe(card, 100, 130);   // dx = +30, below threshold
    await new Promise((r) => setTimeout(r, 400));
    expect(onHide).not.toHaveBeenCalled();
    expect(onSwipeLeft).not.toHaveBeenCalled();
  });

  it("ignores the left-swipe when no onSwipeLeft is provided", async () => {
    const onHide = vi.fn();
    render(
      <SwipeCard onHide={onHide}>
        <div className="msg user">Buy milk</div>
      </SwipeCard>,
    );
    swipe(screen.getByText("Buy milk"), 230, 100);
    await new Promise((r) => setTimeout(r, 400));
    expect(onHide).not.toHaveBeenCalled();
  });
});
