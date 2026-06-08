import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

// Mock the api module so the poller's getStatus() is controllable and no real
// fetch happens. getAccessKey is used by the store's rule-0; default to null.
const getStatusMock = vi.fn(async () => ({ ok: true as const, data: { capabilities: {
  llm: { state: "ready", providers: { anthropic: true, xai: false } },
  embeddings: { state: "ready" }, transcription: { state: "ready" },
  push: { state: "ready" }, geocoder: { state: "absent" }, db: { state: "ready" },
} } }));

vi.mock("./api", () => ({
  getStatus: (...a: any[]) => getStatusMock(...a),
  getAccessKey: () => null,
  clearAccessKey: () => {},
  setAccessKey: () => {},
}));

import { pollDelay, useHealthPoll, refreshNow, __reset } from "./health";

beforeEach(() => {
  getStatusMock.mockClear();
  __reset();
  vi.useFakeTimers();
  // Default: tab visible.
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
});

afterEach(() => {
  vi.useRealTimers();
});

const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

describe("pollDelay (cadence + backoff)", () => {
  it("steady 20s, warming 5s, backoff 5→10→20→40→60→60", () => {
    expect(pollDelay(0, false)).toBe(20000);
    expect(pollDelay(0, true)).toBe(5000);
    expect(pollDelay(1, false)).toBe(5000);
    expect(pollDelay(2, false)).toBe(10000);
    expect(pollDelay(3, false)).toBe(20000);
    expect(pollDelay(4, false)).toBe(40000);
    expect(pollDelay(5, false)).toBe(60000);
    expect(pollDelay(6, false)).toBe(60000);   // capped
  });
});

describe("useHealthPoll", () => {
  it("polls immediately on mount when enabled, and not when disabled", async () => {
    const { unmount } = renderHook(() => useHealthPoll(true));
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(1);
    unmount();

    getStatusMock.mockClear();
    const { unmount: u2 } = renderHook(() => useHealthPoll(false));
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(0);
    u2();
  });

  it("reschedules on the steady 20s cadence", async () => {
    const { unmount } = renderHook(() => useHealthPoll(true));
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(1);   // initial
    await vi.advanceTimersByTimeAsync(20000);
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(2);   // next tick
    unmount();
  });

  it("refreshNow() forces an immediate poll", async () => {
    const { unmount } = renderHook(() => useHealthPoll(true));
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(1);
    refreshNow();
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("stops polling and clears refreshNow after unmount", async () => {
    const { unmount } = renderHook(() => useHealthPoll(true));
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(1);
    unmount();
    getStatusMock.mockClear();
    refreshNow();                              // no registered hook → no-op
    await vi.advanceTimersByTimeAsync(60000);
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(0);
  });

  it("does not network-poll while hidden, but keeps the timer chain alive and resumes", async () => {
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    const { unmount } = renderHook(() => useHealthPoll(true));
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(0);    // hidden → no fetch on mount tick
    // Chain is still alive: advancing time keeps rescheduling (still no fetch while hidden).
    await vi.advanceTimersByTimeAsync(20000);
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(0);
    // Resume → immediate poll.
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    expect(getStatusMock).toHaveBeenCalledTimes(1);
    unmount();
  });
});
