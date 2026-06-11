// Component-integration (Tier 2) tests for the home compose box (Chat.tsx): the
// three-mode segmented control (Entry / Research / Full Brain), the per-mode send
// flow, SSE streaming for the chat modes, and capability/mode gating.
//
// Network: plain JSON endpoints go through MSW (server.use per test); the SSE
// streaming call (streamChat) is awkward over MSW, so it's partially mocked here
// (vi.mock("../api") keeps every other export real, only swapping streamChat for a
// scriptable fake). Capabilities are seeded into the REAL health store via
// ingestVerify() — Chat reads them through useCapability(), not over the wire.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup, fireEvent } from "../test/render";
import { server } from "../test/server";
import type { ChatEvent } from "../api";

// --- streamChat fake -------------------------------------------------------- //
// A per-test holder the mocked streamChat delegates to. Tests overwrite it to
// script the SSE events a turn should emit. Default: resolve with no events.
type StreamArgs = {
  conversationId: number;
  text: string;
  mode: string;
  deep: boolean;
  freshContext: boolean;
};
let streamImpl: (a: StreamArgs, onEvent: (e: ChatEvent) => void) => Promise<void> =
  async () => {};
const streamCalls: StreamArgs[] = [];

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    streamChat: vi.fn(
      async (
        conversationId: number,
        text: string,
        onEvent: (e: ChatEvent) => void,
        _loc?: unknown,
        mode = "assisted",
        freshContext = false,
        deep = false,
      ) => {
        const a = { conversationId, text, mode, deep, freshContext };
        streamCalls.push(a);
        await streamImpl(a, onEvent);
      },
    ),
  };
});

// Imported AFTER the mock factory is declared (vi.mock is hoisted above all imports).
import Chat from "./Chat";
import { __reset, ingestVerify } from "../health";
import { __resetToasts, getToasts } from "../toast";

const READY_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: false } },
  embeddings: { state: "ready" },
  transcription: { state: "ready" },
  push: { state: "ready" },
  geocoder: { state: "ready" },
  db: { state: "ready" },
};
function caps(over: Record<string, any> = {}): any {
  return { capabilities: { ...READY_CAPS, ...over } };
}

// Track which JSON endpoints fired (URL + parsed body) so tests can assert routing
// without reaching into component internals.
let posted: { url: string; method: string; body: any }[] = [];

function recordingHandlers() {
  return [
    // StagingPanel polls this on every mode render — empty = renders nothing.
    http.get("*/api/staging", () => HttpResponse.json([])),
    // Entry capture.
    http.post("*/api/notes/entry", async ({ request }) => {
      posted.push({ url: request.url, method: "POST", body: await request.json() });
      return HttpResponse.json({ title: "notes/My Entry", slug: "my-entry" });
    }),
    // Chat conversation creation (lazy, on the first chat turn).
    http.post("*/api/chat/conversations", ({ request }) => {
      posted.push({ url: request.url, method: "POST", body: null });
      return HttpResponse.json({ id: 42 });
    }),
    // Post-stream re-sync of the authoritative thread.
    http.get("*/api/chat/conversations/:id/messages", () => HttpResponse.json([])),
  ];
}

function entryUrls() {
  return posted.filter((p) => p.url.includes("/api/notes/entry"));
}

beforeEach(() => {
  __reset();
  __resetToasts();
  posted = [];
  streamCalls.length = 0;
  streamImpl = async () => {};
  // jsdom doesn't implement scrollIntoView; Chat calls it in an effect on every
  // message/entry change. Stub it as a no-op so those effects don't throw.
  if (!HTMLElement.prototype.scrollIntoView) {
    HTMLElement.prototype.scrollIntoView = () => {};
  }
  server.use(...recordingHandlers());
  ingestVerify(caps());            // everything ready by default
});
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) The three-mode segmented control
// ---------------------------------------------------------------------------- //
describe("Chat — mode segmented control", () => {
  it("defaults to Research (read-only) on a fresh session", () => {
    renderWithProviders(<Chat />);
    expect(
      screen.getByText(/I only read; I won.t change anything/i),
    ).toBeInTheDocument();
    // Read-only placeholder confirms the active mode.
    expect(
      screen.getByPlaceholderText(/Ask your brain… \(read-only\)/i),
    ).toBeInTheDocument();
  });

  it("switches modes and remembers the choice in sessionStorage", async () => {
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /Full Brain/i }));
    expect(sessionStorage.getItem("jbrain_mode")).toBe("full");
    expect(
      screen.getByPlaceholderText(/full tool access/i),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    expect(sessionStorage.getItem("jbrain_mode")).toBe("entry");
    expect(screen.getByPlaceholderText(/Write an entry/i)).toBeInTheDocument();
  });

  it("resolves a remembered mode from sessionStorage on mount", () => {
    sessionStorage.setItem("jbrain_mode", "full");
    renderWithProviders(<Chat />);
    expect(screen.getByPlaceholderText(/full tool access/i)).toBeInTheDocument();
  });

  it("falls back to the default (research) for a stale/unknown remembered mode", () => {
    sessionStorage.setItem("jbrain_mode", "totally-bogus");
    renderWithProviders(<Chat />);
    expect(
      screen.getByPlaceholderText(/Ask your brain… \(read-only\)/i),
    ).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) Entry mode send → posts to the entry endpoint, shows success
// ---------------------------------------------------------------------------- //
describe("Chat — Entry mode send", () => {
  it("posts the typed text to /api/notes/entry and shows a Saved chip", async () => {
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));

    const box = screen.getByPlaceholderText(/Write an entry/i);
    await user.type(box, "Buy milk");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() => expect(entryUrls()).toHaveLength(1));
    expect(entryUrls()[0].body.text).toBe("Buy milk");

    // Success surfaces as the "Saved:" chip linking to the new note.
    await screen.findByText(/Saved:/i);
    expect(screen.queryByText(/Saving…/i)).not.toBeInTheDocument();
    // Entry mode is a pure write — it must never drive the chat/stream path.
    expect(streamCalls).toHaveLength(0);
  });

  it("hides a saved entry card from view on a right-swipe (no DB call)", async () => {
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await user.type(screen.getByPlaceholderText(/Write an entry/i), "Buy milk");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));
    await screen.findByText(/Saved:/i);

    const before = entryUrls().length;   // one POST for the save itself
    const card = screen.getByText("Buy milk");
    // A clear, horizontal-dominant rightward drag, clear of the left OS edge zone.
    fireEvent.touchStart(card, { touches: [{ clientX: 100, clientY: 100 }] });
    fireEvent.touchEnd(card, { changedTouches: [{ clientX: 220, clientY: 104 }] });

    // The card (bubble + chip) leaves the view…
    await waitFor(() => expect(screen.queryByText("Buy milk")).not.toBeInTheDocument());
    expect(screen.queryByText(/Saved:/i)).not.toBeInTheDocument();
    // …and hiding is view-only: it never hits the entry endpoint again.
    expect(entryUrls()).toHaveLength(before);
  });

  it("leaves the card in place when the swipe is too short to count", async () => {
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await user.type(screen.getByPlaceholderText(/Write an entry/i), "Buy milk");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));
    await screen.findByText(/Saved:/i);

    const card = screen.getByText("Buy milk");
    fireEvent.touchStart(card, { touches: [{ clientX: 100, clientY: 100 }] });
    fireEvent.touchEnd(card, { changedTouches: [{ clientX: 110, clientY: 100 }] }); // dx=10, below threshold

    expect(screen.getByText("Buy milk")).toBeInTheDocument();
  });

  it("rolls back and toasts when the entry POST fails", async () => {
    server.use(
      http.post("*/api/notes/entry", () =>
        HttpResponse.json({ detail: "disk full" }, { status: 500 }),
      ),
    );
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await user.type(screen.getByPlaceholderText(/Write an entry/i), "Buy milk");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    // The <Toaster> view lives in App (not mounted here), so assert against the toast
    // store the component pushed the failure into — and that no Saved chip appeared.
    await waitFor(() =>
      expect(getToasts().some((t) => /Couldn.t save entry/i.test(t.message))).toBe(true),
    );
    expect(screen.queryByText(/Saved:/i)).not.toBeInTheDocument();
    // The optimistic "Saving…" row is rolled back on failure.
    expect(screen.queryByText(/Saving…/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (c) Research mode is read-only
// ---------------------------------------------------------------------------- //
describe("Chat — Research mode (read-only)", () => {
  it("sends via the research stream path and never touches the entry endpoint", async () => {
    streamImpl = async (_a, onEvent) => {
      onEvent({ type: "token", text: "Read-only answer." });
    };
    const { user } = renderWithProviders(<Chat />);
    // Research is the default mode — no mode click needed.
    await user.type(
      screen.getByPlaceholderText(/Ask your brain… \(read-only\)/i),
      "what did I eat",
    );
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() => expect(streamCalls).toHaveLength(1));
    expect(streamCalls[0].mode).toBe("research");
    expect(streamCalls[0].text).toBe("what did I eat");
    // Read-only: a conversation may be created for the thread, but NO write to entry.
    expect(entryUrls()).toHaveLength(0);
    // The streamed reply renders.
    // Streamed reply renders via a typewriter; allow time under load (no flake).
    await screen.findByText(/Read-only answer\./i, {}, { timeout: 4000 });
  });

  it("forwards the Deep opt-in to the research stream when toggled on", async () => {
    const { user } = renderWithProviders(<Chat />);
    // The bolt "Deep" toggle is Research-only.
    await user.click(screen.getByRole("button", { name: /Deep/i }));
    await user.type(
      screen.getByPlaceholderText(/Ask your brain… \(read-only\)/i),
      "trace my labs",
    );
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() => expect(streamCalls).toHaveLength(1));
    expect(streamCalls[0].mode).toBe("research");
    expect(streamCalls[0].deep).toBe(true);
  });
});

// ---------------------------------------------------------------------------- //
// (d) Full Brain send → drives the chat/stream path and renders the streamed reply
// ---------------------------------------------------------------------------- //
describe("Chat — Full Brain mode", () => {
  it("creates a conversation, streams in 'assisted' mode, and renders the reply", async () => {
    streamImpl = async (_a, onEvent) => {
      onEvent({ type: "token", text: "Hello " });
      onEvent({ type: "token", text: "from the brain." });
    };
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /Full Brain/i }));

    await user.type(
      screen.getByPlaceholderText(/full tool access/i),
      "capture this thought",
    );
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    // The lazy conversation create fired before the stream.
    await waitFor(() =>
      expect(posted.some((p) => p.url.includes("/api/chat/conversations"))).toBe(true),
    );
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    expect(streamCalls[0].mode).toBe("assisted");
    expect(streamCalls[0].conversationId).toBe(42);

    // The optimistic user bubble and the streamed assistant reply both render.
    await screen.findByText("capture this thought");
    await screen.findByText(/Hello from the brain\./i, {}, { timeout: 4000 });
  });

  it("renders an in-stream error event on the assistant bubble", async () => {
    streamImpl = async (_a, onEvent) => {
      onEvent({ type: "error", message: "model unavailable" });
    };
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /Full Brain/i }));
    await user.type(screen.getByPlaceholderText(/full tool access/i), "hi");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await screen.findByText(/model unavailable/i, {}, { timeout: 4000 });
  });
});

// ---------------------------------------------------------------------------- //
// (e) Capability / mode gating
// ---------------------------------------------------------------------------- //
describe("Chat — capability/mode gating", () => {
  it("disables Send and explains why in a chat mode when the LLM is absent", async () => {
    __reset();
    ingestVerify(caps({ llm: { state: "absent", providers: { anthropic: false, xai: false } } }));
    const { user } = renderWithProviders(<Chat />);   // defaults to Research

    await user.type(
      screen.getByPlaceholderText(/Ask your brain… \(read-only\)/i),
      "anything",
    );
    const send = screen.getByRole("button", { name: /need an API key|Send/i }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    // The safety line shows the capability reason rather than the scope hint.
    await screen.findByText(/need an API key/i);

    // Even a forced click is a no-op — nothing streams.
    await user.click(send);
    expect(streamCalls).toHaveLength(0);
  });

  it("still allows Entry (a non-AI write) when the LLM is absent", async () => {
    __reset();
    ingestVerify(caps({ llm: { state: "absent", providers: { anthropic: false, xai: false } } }));
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));

    await user.type(screen.getByPlaceholderText(/Write an entry/i), "offline note");
    const send = screen.getByRole("button", { name: /^Send$/i }) as HTMLButtonElement;
    expect(send.disabled).toBe(false);
    await user.click(send);

    await waitFor(() => expect(entryUrls()).toHaveLength(1));
    expect(entryUrls()[0].body.text).toBe("offline note");
  });
});
