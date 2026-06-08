import { afterEach, describe, expect, it, vi } from "vitest";
import { getStatus, setAccessKey, clearAccessKey } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAccessKey();
});

describe("getStatus", () => {
  it("returns the parsed body on a 200 (full doc or skeleton)", async () => {
    const body = { ok: true, brain: "B", ts: "x", capabilities: { llm: { state: "ready" } } };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
    const r = await getStatus();
    expect(r).toEqual({ ok: true, data: body });
  });

  it("maps a 5xx to unreachable (does not throw)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("err", { status: 503 })));
    const r = await getStatus();
    expect(r).toEqual({ ok: false, reason: "unreachable" });
  });

  it("maps a network rejection / abort to unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("network down"); }));
    const r = await getStatus();
    expect(r).toEqual({ ok: false, reason: "unreachable" });
  });

  it("aborts after the timeout and returns unreachable (no hang)", async () => {
    vi.useFakeTimers();
    // fetch that rejects when its signal aborts (mimics the platform behaviour).
    vi.stubGlobal("fetch", vi.fn((_url: any, init: any) => new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })));
    const p = getStatus(8000);
    await vi.advanceTimersByTimeAsync(8000);   // fire the abort timer
    await expect(p).resolves.toEqual({ ok: false, reason: "unreachable" });
    vi.useRealTimers();
  });

  it("sends the bearer key and targets /api/system/status", async () => {
    setAccessKey("secret-key");
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await getStatus();
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/system/status");
    expect((init.headers as any)["Authorization"]).toBe("Bearer secret-key");
  });
});
