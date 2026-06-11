import { describe, expect, it } from "vitest";
import { EDGE_GUARD, SWIPE_MIN_DX, shouldHideOnSwipe, shouldOpenHistoryOnSwipe } from "./swipeGesture";

const W = 400;          // a phone-ish viewport width for the edge-guard math
const MID = W / 2;      // a start x clear of both screen edges

describe("shouldOpenHistoryOnSwipe (left-swipe → open history)", () => {
  it("fires for a clear, horizontal-dominant leftward drag clear of the edges", () => {
    const start = { x: MID, y: 100 };
    const end = { x: MID - SWIPE_MIN_DX - 10, y: 105 };
    expect(shouldOpenHistoryOnSwipe(start, end, W, 0)).toBe(true);
  });
  it("ignores a too-short drag", () => {
    const start = { x: MID, y: 100 };
    const end = { x: MID - (SWIPE_MIN_DX - 1), y: 100 };
    expect(shouldOpenHistoryOnSwipe(start, end, W, 0)).toBe(false);
  });
  it("ignores a rightward drag (that's the hide gesture)", () => {
    const start = { x: MID, y: 100 };
    const end = { x: MID + SWIPE_MIN_DX + 10, y: 100 };
    expect(shouldOpenHistoryOnSwipe(start, end, W, 0)).toBe(false);
  });
  it("ignores a mostly-vertical drag (a scroll)", () => {
    const start = { x: MID, y: 100 };
    const end = { x: MID - SWIPE_MIN_DX - 10, y: 100 + (SWIPE_MIN_DX + 10) }; // dy ~= |dx|
    expect(shouldOpenHistoryOnSwipe(start, end, W, 0)).toBe(false);
  });
  it("ignores a swipe starting in the right OS edge zone", () => {
    const start = { x: W - EDGE_GUARD + 1, y: 100 };
    const end = { x: start.x - SWIPE_MIN_DX - 10, y: 100 };
    expect(shouldOpenHistoryOnSwipe(start, end, W, 0)).toBe(false);
  });
  it("ignores the gesture while text is selected", () => {
    const start = { x: MID, y: 100 };
    const end = { x: MID - SWIPE_MIN_DX - 10, y: 100 };
    expect(shouldOpenHistoryOnSwipe(start, end, W, 5)).toBe(false);
  });
});

describe("shouldHideOnSwipe (right-swipe → hide card from view)", () => {
  it("fires for a clear, horizontal-dominant rightward drag clear of the edges", () => {
    const start = { x: MID, y: 100 };
    const end = { x: MID + SWIPE_MIN_DX + 10, y: 105 };
    expect(shouldHideOnSwipe(start, end, 0)).toBe(true);
  });
  it("ignores a too-short drag", () => {
    const start = { x: MID, y: 100 };
    const end = { x: MID + (SWIPE_MIN_DX - 1), y: 100 };
    expect(shouldHideOnSwipe(start, end, 0)).toBe(false);
  });
  it("ignores a leftward drag (that's the open-history gesture)", () => {
    const start = { x: MID, y: 100 };
    const end = { x: MID - SWIPE_MIN_DX - 10, y: 100 };
    expect(shouldHideOnSwipe(start, end, 0)).toBe(false);
  });
  it("ignores a mostly-vertical drag (a scroll)", () => {
    const start = { x: MID, y: 100 };
    const end = { x: MID + SWIPE_MIN_DX + 10, y: 100 + (SWIPE_MIN_DX + 10) };
    expect(shouldHideOnSwipe(start, end, 0)).toBe(false);
  });
  it("ignores a swipe starting in the left OS edge (back-gesture) zone", () => {
    const start = { x: EDGE_GUARD - 1, y: 100 };
    const end = { x: start.x + SWIPE_MIN_DX + 10, y: 100 };
    expect(shouldHideOnSwipe(start, end, 0)).toBe(false);
  });
  it("ignores the gesture while text is selected", () => {
    const start = { x: MID, y: 100 };
    const end = { x: MID + SWIPE_MIN_DX + 10, y: 100 };
    expect(shouldHideOnSwipe(start, end, 5)).toBe(false);
  });
});
