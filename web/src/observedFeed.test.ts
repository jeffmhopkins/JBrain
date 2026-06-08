// Phase 4 — observed-health feed: api()/streamChat/streamSSE feed real request
// outcomes into the health store. Asserts the OBSERVABLE store effect (not spies),
// and the R3-M2 stall-vs-user-abort distinction.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, rebuildStream, streamChat } from "./api";
import { __reset, getSnapshot, ingestVerify } from "./health";

let clock = 1_000_000;
const now = () => clock;

function fullCaps(): any {
  return { capabilities: {
    llm: { state: "ready", verified: null, providers: { anthropic: true, xai: false } },
    embeddings: { state: "ready" }, transcription: { state: "ready" },
    push: { state: "ready" }, geocoder: { state: "absent" }, db: { state: "ready" },
  } };
}

beforeEach(() => {
  clock = 1_000_000;
  __reset(now);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// --- api() ----------------------------------------------------------------- //

describe("api() observed feed", () => {
  it("reports neterr on a fetch rejection (and still throws)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("down"); }));
    await expect(api("/x")).rejects.toThrow("down");
    expect(getSnapshot().observed.lastNetErrAt).toBe(clock);
  });

  it("stamps lastOkAt on any HTTP response", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    await api("/x");
    expect(getSnapshot().lastOkAt).toBe(clock);
  });

  it("marks last5xxAt on a 5xx and throws a categorized ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "boom" }), { status: 503 })));
    const err = await api("/x").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(503);
    expect(err.category).toBe("unavailable");
    expect(getSnapshot().observed.last5xxAt).toBe(clock);
    expect(getSnapshot().lastOkAt).toBe(clock);   // a 5xx is still a server byte (reachable)
  });
});

describe("ApiError.category", () => {
  it("maps status to category", () => {
    expect(new ApiError("", 401).category).toBe("auth");
    expect(new ApiError("", 403).category).toBe("auth");
    expect(new ApiError("", 422).category).toBe("validation");
    expect(new ApiError("", 503).category).toBe("unavailable");
    expect(new ApiError("", 500).category).toBe("server");
  });
});

// --- streamChat ------------------------------------------------------------ //

function sse(obj: unknown): string {
  return `data: ${JSON.stringify(obj)}\n\n`;
}

function scriptedBody(chunks: string[]) {
  let i = 0;
  const enc = new TextEncoder();
  return {
    getReader() {
      return {
        async read() {
          if (i < chunks.length) return { done: false, value: enc.encode(chunks[i++]) };
          return { done: true, value: undefined };
        },
        async cancel() {},
      };
    },
  };
}

describe("streamChat observed LLM health", () => {
  it("an error event downgrades a ready LLM to degraded; a done event heals it", async () => {
    ingestVerify(fullCaps());
    expect(getSnapshot().caps?.llm.state).toBe("ready");

    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true, status: 200, body: scriptedBody([sse({ type: "token", text: "hi" }), sse({ type: "error", message: "x" })]),
    })));
    const events: any[] = [];
    await streamChat(1, "hello", (e) => events.push(e));
    expect(getSnapshot().observed.llmFailAt).toBe(clock);
    expect(getSnapshot().caps?.llm.state).toBe("degraded");
    expect(events.map((e) => e.type)).toEqual(["token", "error"]);   // events still delivered

    clock += 5;
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true, status: 200, body: scriptedBody([sse({ type: "done" })]),
    })));
    await streamChat(1, "again", () => {});
    expect(getSnapshot().observed.llmOkAt).toBe(clock);
    expect(getSnapshot().caps?.llm.state).toBe("ready");           // healed
  });
});

describe("streamChat stall vs user-abort (R3-M2)", () => {
  it("reports stall ONLY when the watchdog fires, never on a user abort", async () => {
    // A reader whose read() hangs until the (internal) fetch signal aborts.
    vi.useFakeTimers();
    let capturedSignal: AbortSignal | null = null;
    vi.stubGlobal("fetch", vi.fn(async (_url: any, init: any) => {
      capturedSignal = init.signal;
      let first = true;
      return {
        ok: true, status: 200,
        body: {
          getReader() {
            return {
              read() {
                if (first) { first = false; return Promise.resolve({ done: false, value: new TextEncoder().encode(sse({ type: "token", text: "a" })) }); }
                return new Promise((_resolve, reject) => {
                  const fail = () => reject(new DOMException("aborted", "AbortError"));
                  if (capturedSignal!.aborted) return fail();   // already aborted → reject now (no missed event)
                  capturedSignal!.addEventListener("abort", fail);
                });
              },
              async cancel() {},
            };
          },
        },
      };
    }));

    const p = streamChat(1, "hello", () => {});
    await vi.advanceTimersByTimeAsync(90000);   // fire the 90s stall watchdog
    await p;
    expect(getSnapshot().observed.lastStallAt).toBe(clock);   // real stall reported
    expect(getSnapshot().observed.lastNetErrAt).toBeUndefined();
  });

  it("a user-initiated abort reports nothing", async () => {
    const external = new AbortController();
    let capturedSignal: AbortSignal | null = null;
    vi.stubGlobal("fetch", vi.fn(async (_url: any, init: any) => {
      capturedSignal = init.signal;
      let first = true;
      return {
        ok: true, status: 200,
        body: {
          getReader() {
            return {
              read() {
                if (first) { first = false; return Promise.resolve({ done: false, value: new TextEncoder().encode(sse({ type: "token", text: "a" })) }); }
                return new Promise((_resolve, reject) => {
                  const fail = () => reject(new DOMException("aborted", "AbortError"));
                  if (capturedSignal!.aborted) return fail();   // already aborted → reject now (no missed event)
                  capturedSignal!.addEventListener("abort", fail);
                });
              },
              async cancel() {},
            };
          },
        },
      };
    }));

    const p = streamChat(1, "hello", () => {}, null, "assisted", false, false, external.signal);
    await new Promise((r) => setTimeout(r, 0));   // let it read the first chunk + await the second
    external.abort();                              // USER leaves the chat
    await p;
    expect(getSnapshot().observed.lastStallAt).toBeUndefined();   // NOT a stall
    expect(getSnapshot().observed.lastNetErrAt).toBeUndefined();  // NOT a net error
  });

  it("streamChat propagates a network error to the caller AND reports neterr", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("conn refused"); }));
    await expect(streamChat(1, "hi", () => {})).rejects.toThrow("conn refused");
    expect(getSnapshot().observed.lastNetErrAt).toBe(clock);
  });
});

describe("streamSSE / rebuild observed LLM health (R3-M5)", () => {
  it("a rebuild error event reports llm-fail (degrading a ready LLM)", async () => {
    ingestVerify(fullCaps());
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true, status: 200, body: scriptedBody([sse({ type: "error", message: "rate limited" })]),
    })));
    await rebuildStream("some-slug", () => {}).done;
    expect(getSnapshot().observed.llmFailAt).toBe(clock);
    expect(getSnapshot().caps?.llm.state).toBe("degraded");
  });

  it("a clean rebuild done reports llm-ok", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true, status: 200, body: scriptedBody([sse({ type: "done", draft: "" })]),
    })));
    await rebuildStream("some-slug", () => {}).done;
    expect(getSnapshot().observed.llmOkAt).toBe(clock);
  });
});
