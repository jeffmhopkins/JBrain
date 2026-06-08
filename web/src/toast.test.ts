import { beforeEach, describe, expect, it, vi } from "vitest";
import { __resetToasts, dismissToast, explainError, getToasts, showToast } from "./toast";
import { ApiError } from "./api";

let clock = 1000;
const now = () => clock;

beforeEach(() => { clock = 1000; __resetToasts(now); });

describe("toast store", () => {
  it("adds a toast and returns its id", () => {
    const id = showToast("boom");
    expect(id).not.toBeNull();
    expect(getToasts()).toHaveLength(1);
    expect(getToasts()[0]).toMatchObject({ message: "boom", tone: "error" });
  });

  it("de-dupes an identical message within the window", () => {
    showToast("same");
    const second = showToast("same");        // within DEDUPE_MS
    expect(second).toBeNull();
    expect(getToasts()).toHaveLength(1);
    clock += 5000;                           // past DEDUPE_MS
    expect(showToast("same")).not.toBeNull();
    expect(getToasts()).toHaveLength(2);
  });

  it("allows a deliberate repeat just past the (short) dedupe window", () => {
    expect(showToast("Couldn't send")).not.toBeNull();
    clock += 1499;
    expect(showToast("Couldn't send")).toBeNull();   // still within the burst window
    clock += 2;                                       // now past 1500ms → a real retry shows
    expect(showToast("Couldn't send")).not.toBeNull();
  });

  it("distinct messages/tones are not de-duped", () => {
    showToast("a"); showToast("b"); showToast("a", "info");
    expect(getToasts()).toHaveLength(3);
  });

  it("caps the stack at the max", () => {
    for (let i = 0; i < 8; i++) showToast(`m${i}`);
    expect(getToasts().length).toBeLessThanOrEqual(4);
    expect(getToasts().at(-1)?.message).toBe("m7");   // newest kept
  });

  it("dismiss removes by id", () => {
    const id = showToast("x")!;
    dismissToast(id);
    expect(getToasts()).toHaveLength(0);
  });

  it("auto-dismisses after the timeout", () => {
    vi.useFakeTimers();
    showToast("temp");
    expect(getToasts()).toHaveLength(1);
    vi.advanceTimersByTime(6000);
    expect(getToasts()).toHaveLength(0);
    vi.useRealTimers();
  });
});

describe("explainError", () => {
  it("prefers the server detail from an ApiError", () => {
    expect(explainError(new ApiError("Name required", 400))).toBe("Name required");
  });
  it("uses Error.message", () => {
    expect(explainError(new Error("network down"))).toBe("network down");
  });
  it("falls back for unknown values", () => {
    expect(explainError(null, "fallback")).toBe("fallback");
    expect(explainError(undefined)).toBe("Something went wrong.");
  });
  it("passes through a non-empty string", () => {
    expect(explainError("plain message")).toBe("plain message");
  });
});
