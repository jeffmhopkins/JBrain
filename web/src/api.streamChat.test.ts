import { afterEach, describe, expect, it, vi } from "vitest";
import { streamChat, clearAccessKey } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  clearAccessKey();
});

describe("streamChat initial-fetch (headers) watchdog", () => {
  it("aborts and rejects if the server never returns response headers (no infinite 'Thinking…')", async () => {
    vi.useFakeTimers();
    // A fetch that never resolves on its own — only rejects when its signal aborts (the
    // platform behaviour). Without the headers watchdog this would hang forever, leaving the
    // caller's `streaming` latch set and the composer stuck.
    vi.stubGlobal("fetch", vi.fn((_url: any, init: any) => new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })));
    const onEvent = vi.fn();
    const p = streamChat(1, "hi", onEvent).catch((e) => e);
    await vi.advanceTimersByTimeAsync(30000);   // fire the headers watchdog
    const err = await p;
    expect(err).toBeDefined();                  // streamChat rejected rather than hanging
    expect(String(err)).toMatch(/abort/i);      // ...via the headers-watchdog abort
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("does not abort a fetch that returns headers promptly", async () => {
    vi.useFakeTimers();
    // Headers arrive immediately with a normal (empty) SSE body that closes — the watchdog must
    // be cleared and not abort the established stream.
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 200 })));
    const onEvent = vi.fn();
    const p = streamChat(1, "hi", onEvent);
    await vi.advanceTimersByTimeAsync(60000);   // well past the 30s headers window
    await expect(p).resolves.toBeUndefined();   // completed normally, no abort-driven throw
  });
});

describe("streamChat `done` is authoritative", () => {
  it("resolves on a `done` event even if the server holds the connection open after", async () => {
    // A body that emits a token + `done`, then never closes — a wedged proxy/server holding the
    // socket open. Pre-fix the read loop would block on the next read() (until the 90s watchdog or
    // forever), leaving the composer stuck; post-fix `done` breaks the loop and streamChat resolves.
    const enc = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(enc.encode('data: {"type":"token","text":"hi"}\n\n'));
        controller.enqueue(enc.encode('data: {"type":"done"}\n\n'));
        // deliberately never call controller.close()
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, { status: 200 })));
    const events: any[] = [];
    await streamChat(1, "hi", (e) => events.push(e));   // must resolve, not hang
    expect(events.map((e) => e.type)).toEqual(["token", "done"]);
  });
});
