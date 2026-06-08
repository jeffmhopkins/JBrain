// Extended component-integration (Tier 2) tests for the home compose box (Chat.tsx),
// covering the SECONDARY flows the base Chat.test.tsx doesn't reach: attachment
// upload from the compose box (Entry + Full Brain), the Entry sub-selector
// (Generic / Medical / Financial destinations + inline add), the geo-stamp toggle,
// the /clear conversation-reset path, quick-op undo, and the external-lookup gate.
//
// Same established standard as Chat.test.tsx: renderWithProviders, MSW (server.use
// per test, onUnhandledRequest "error"), vi.mock("../api") keeping all real except a
// scripted streamChat, ingestVerify() to seed capabilities, scrollIntoView stubbed.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import type { ChatEvent } from "../api";

// --- streamChat fake -------------------------------------------------------- //
// Captures the FULL real signature so a test can assert the geo coords (param 4)
// that the compose box stamps onto a turn. Tests overwrite streamImpl to script the
// SSE events a turn emits.
//
// uploadAttachment is ALSO mocked here: it's XHR-based, which MSW can't observe (a real
// call just hangs under jsdom — see Attachments.test.tsx, which mocks it for the same
// reason). The mock records the (slug, file) it was handed and resolves successfully, so
// the upload-then-save / upload-then-stream flows complete and tests assert the routing.
type StreamArgs = {
  conversationId: number;
  text: string;
  loc: { lat: number; lon: number } | null;
  mode: string;
  deep: boolean;
  freshContext: boolean;
};
let streamImpl: (a: StreamArgs, onEvent: (e: ChatEvent) => void) => Promise<void> =
  async () => {};
const streamCalls: StreamArgs[] = [];
// Recorded uploadAttachment calls (slug + file name) for the attachment-flow assertions.
const uploadCalls: { slug: string; name: string }[] = [];

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    streamChat: vi.fn(
      async (
        conversationId: number,
        text: string,
        onEvent: (e: ChatEvent) => void,
        loc?: { lat: number; lon: number } | null,
        mode = "assisted",
        freshContext = false,
        deep = false,
      ) => {
        const a = { conversationId, text, loc: loc ?? null, mode, deep, freshContext };
        streamCalls.push(a);
        await streamImpl(a, onEvent);
      },
    ),
    uploadAttachment: vi.fn(
      async (slug: string, file: File, onProgress?: (p: number) => void) => {
        uploadCalls.push({ slug, name: file.name });
        onProgress?.(100);
        return { id: 7 };
      },
    ),
  };
});

// Imported AFTER the mock factory (vi.mock is hoisted above all imports).
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

// Track which JSON/multipart endpoints fired so tests assert routing without
// reaching into component internals.
let posted: { url: string; method: string; body: any }[] = [];

function recordingHandlers() {
  return [
    http.get("*/api/staging", () => HttpResponse.json([])),
    http.post("*/api/notes/entry", async ({ request }) => {
      posted.push({ url: request.url, method: "POST", body: await request.json() });
      return HttpResponse.json({ title: "notes/My Entry", slug: "my-entry" });
    }),
    http.post("*/api/chat/conversations", ({ request }) => {
      posted.push({ url: request.url, method: "POST", body: null });
      return HttpResponse.json({ id: 42 });
    }),
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
  uploadCalls.length = 0;
  streamImpl = async () => {};
  if (!HTMLElement.prototype.scrollIntoView) {
    HTMLElement.prototype.scrollIntoView = () => {};
  }
  // Geo OFF by default (most flows don't stamp). The geo test opts in explicitly.
  localStorage.removeItem("jbrain_geo");
  server.use(...recordingHandlers());
  ingestVerify(caps());
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

// ---------------------------------------------------------------------------- //
// (a) Compose-box attachment upload
// ---------------------------------------------------------------------------- //
describe("Chat — compose-box attachment upload", () => {
  // Pick a file into the hidden <input type=file> behind the clip button.
  async function attach(user: ReturnType<typeof renderWithProviders>["user"], file: File) {
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, file);
  }

  it("stages a picked file as a chip and removes it on ✕", async () => {
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await attach(user, new File(["hi"], "photo.jpg", { type: "image/jpeg" }));

    // The chip with the filename appears in the composer.
    await screen.findByText("photo.jpg");
    // Remove it again.
    await user.click(screen.getByRole("button", { name: "✕" }));
    await waitFor(() => expect(screen.queryByText("photo.jpg")).not.toBeInTheDocument());
  });

  it("rejects an over-100MB file with an info toast and stages nothing", async () => {
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    const huge = new File(["x"], "huge.bin", { type: "application/octet-stream" });
    // 100MB + 1 byte — patch size (user.upload won't synthesize a giant buffer).
    Object.defineProperty(huge, "size", { value: 100 * 1024 * 1024 + 1 });
    await attach(user, huge);

    await waitFor(() =>
      expect(getToasts().some((t) => /Over 100 MB \(skipped\)/i.test(t.message))).toBe(true),
    );
    expect(screen.queryByText("huge.bin")).not.toBeInTheDocument();
  });

  it("Entry send with an attachment creates the note then uploads the file", async () => {
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await attach(user, new File(["data"], "scan.png", { type: "image/png" }));
    await screen.findByText("scan.png");

    await user.type(screen.getByPlaceholderText(/Write an entry/i), "lab photo");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    // Note first, then the attachment lands on its slug.
    await waitFor(() => expect(entryUrls()).toHaveLength(1));
    await waitFor(() => expect(uploadCalls).toHaveLength(1));
    expect(uploadCalls[0].slug).toBe("my-entry");
    expect(uploadCalls[0].name).toBe("scan.png");
    await screen.findByText(/Saved:/i);
  });

  it("Full Brain send with an attachment makes a carrier note, uploads, then streams", async () => {
    streamImpl = async (_a, onEvent) => onEvent({ type: "token", text: "Got the file." });
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /Full Brain/i }));
    await attach(user, new File(["d"], "report.pdf", { type: "application/pdf" }));
    await screen.findByText("report.pdf");

    await user.type(screen.getByPlaceholderText(/full tool access/i), "file this");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    // A carrier note is created for the file, the file is uploaded, then the turn streams.
    await waitFor(() => expect(entryUrls()).toHaveLength(1));
    await waitFor(() => expect(uploadCalls).toHaveLength(1));
    expect(uploadCalls[0].name).toBe("report.pdf");
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    expect(streamCalls[0].mode).toBe("assisted");
    // The saved-note wikilink is folded into the streamed turn's user text.
    expect(streamCalls[0].text).toMatch(/I attached a file/i);
    await screen.findByText(/Got the file\./i);
  });
});

// ---------------------------------------------------------------------------- //
// (b) Entry sub-selector — Generic / Medical / Financial destinations
// ---------------------------------------------------------------------------- //
describe("Chat — Entry sub-selector & destinations", () => {
  it("expands the sub-picker on re-press and loads medical destinations", async () => {
    server.use(
      http.get("*/api/medical/destinations", () =>
        HttpResponse.json({ names: ["2026-03 Admission", "Cardiology"] }),
      ),
    );
    const { user } = renderWithProviders(<Chat />);
    // First press selects Entry; re-press expands into the sub-picker.
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    // The morphed row now offers Medical / Financial.
    await user.click(screen.getByRole("button", { name: /Medical/i }));

    // The destination picklist loads and the dest path label shows.
    await screen.findByText("notes/medical/");
    await screen.findByRole("option", { name: "2026-03 Admission" });
  });

  it("routes a Medical entry to the chosen destination under root=medical", async () => {
    server.use(
      http.get("*/api/medical/destinations", () =>
        HttpResponse.json({ names: ["Cardiology"] }),
      ),
    );
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await user.click(screen.getByRole("button", { name: /Medical/i }));
    await screen.findByRole("option", { name: "Cardiology" });

    await user.type(screen.getByPlaceholderText(/Log a lab/i), "BP 120/80");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() => expect(entryUrls()).toHaveLength(1));
    expect(entryUrls()[0].body.text).toBe("BP 120/80");
    expect(entryUrls()[0].body.root).toBe("medical");
    expect(entryUrls()[0].body.dest).toBe("Cardiology");
  });

  it("blocks a Medical send with no destination and shows an info toast", async () => {
    // Empty destination list → curDest stays "" → send is gated.
    server.use(
      http.get("*/api/medical/destinations", () => HttpResponse.json({ names: [] })),
    );
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await user.click(screen.getByRole("button", { name: /Medical/i }));
    await screen.findByText(/\(add a destination\)/i);

    await user.type(screen.getByPlaceholderText(/Log a lab/i), "no dest");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() =>
      expect(getToasts().some((t) => /Pick or add a medical destination/i.test(t.message))).toBe(true),
    );
    expect(entryUrls()).toHaveLength(0);
  });

  it("adds a Financial destination inline via the prompt and selects it", async () => {
    server.use(
      http.get("*/api/financial/destinations", () => HttpResponse.json({ names: [] })),
      http.put("*/api/financial/destinations", async ({ request }) => {
        posted.push({ url: request.url, method: "PUT", body: await request.json() });
        return HttpResponse.json({ names: ["2026 Brokerage"] });
      }),
    );
    vi.spyOn(window, "prompt").mockReturnValue("2026 Brokerage");
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await user.click(screen.getByRole("button", { name: /^Entry$/i }));
    await user.click(screen.getByRole("button", { name: /Financial/i }));
    await screen.findByText("notes/financial/");

    await user.click(screen.getByRole("button", { name: /New/i }));

    await waitFor(() =>
      expect(posted.some((p) => p.method === "PUT" && /financial\/destinations/.test(p.url))).toBe(true),
    );
    await screen.findByRole("option", { name: "2026 Brokerage" });
    expect(localStorage.getItem("jbrain_fin_dest")).toBe("2026 Brokerage");
  });
});

// ---------------------------------------------------------------------------- //
// (c) Geo-stamp toggle — coords are passed through to the stream when geo is on
// ---------------------------------------------------------------------------- //
describe("Chat — geo stamp", () => {
  it("stamps the turn with coords when geolocation is enabled", async () => {
    localStorage.setItem("jbrain_geo", "1");
    // jsdom has no geolocation; provide a fix.
    (navigator as any).geolocation = {
      getCurrentPosition: (ok: PositionCallback) =>
        ok({ coords: { latitude: 37.7749, longitude: -122.4194 } } as GeolocationPosition),
    };
    streamImpl = async (_a, onEvent) => onEvent({ type: "token", text: "ok" });

    const { user } = renderWithProviders(<Chat />);   // Research default
    await user.type(screen.getByPlaceholderText(/Ask your brain/i), "where am I");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() => expect(streamCalls).toHaveLength(1));
    expect(streamCalls[0].loc).toEqual({ lat: 37.7749, lon: -122.4194 });
  });

  it("sends with no coords (null) when geolocation is disabled", async () => {
    streamImpl = async (_a, onEvent) => onEvent({ type: "token", text: "ok" });
    const { user } = renderWithProviders(<Chat />);
    await user.type(screen.getByPlaceholderText(/Ask your brain/i), "no geo");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() => expect(streamCalls).toHaveLength(1));
    expect(streamCalls[0].loc).toBeNull();
  });
});

// ---------------------------------------------------------------------------- //
// (d) /clear — resets the conversation & wipes stored tool-call history
// ---------------------------------------------------------------------------- //
describe("Chat — /clear reset", () => {
  it("clears the thread, starts a new conversation, and wipes the old step log", async () => {
    // Seed an existing conversation so /clear has an OLD id to clean up.
    localStorage.setItem("jbrain_conv_chat", "99");
    server.use(
      http.get("*/api/chat/conversations/99/messages", () =>
        HttpResponse.json([{ id: 1, role: "assistant", content: "old reply", step_count: 0 }]),
      ),
      http.delete("*/api/chat/conversations/:id/steps", ({ params }) => {
        posted.push({ url: `/steps/${params.id}`, method: "DELETE", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /Full Brain/i }));
    // The seeded thread restores.
    await screen.findByText(/old reply/i);

    await user.type(screen.getByPlaceholderText(/full tool access/i), "/clear");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    // A brand-new conversation is created and the old step log is wiped (by old id 99).
    await waitFor(() =>
      expect(posted.some((p) => p.method === "DELETE" && /\/steps\/99/.test(p.url))).toBe(true),
    );
    await waitFor(() =>
      expect(posted.some((p) => p.url.includes("/api/chat/conversations") && p.method === "POST")).toBe(true),
    );
    // The cleared reply is gone and /clear never streamed a turn.
    await waitFor(() => expect(screen.queryByText(/old reply/i)).not.toBeInTheDocument());
    expect(streamCalls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------- //
// (e) Quick-op undo — an applied-action chip's Undo posts and flips to "undone"
// ---------------------------------------------------------------------------- //
describe("Chat — quick-op undo", () => {
  it("undoes an applied action persisted as an event row", async () => {
    // The transient `applied` chip shown DURING a stream is cleared by the post-stream
    // re-sync (send()'s finally), which reloads the authoritative thread — where the
    // applied action lives as a persisted 'event' row carrying its undo_id. Return that
    // row from the messages re-sync so the undoable chip survives, exercising the event-
    // row render + undo branch.
    streamImpl = async (_a, onEvent) => onEvent({ type: "token", text: "Done." });
    server.use(
      http.get("*/api/chat/conversations/:id/messages", () =>
        HttpResponse.json([
          { id: 1, role: "user", content: "make a note", step_count: 0 },
          { id: 2, role: "assistant", content: "Done.", step_count: 0 },
          {
            id: 3,
            role: "event",
            content: JSON.stringify({ summary: "Created note Foo", undo_id: 5 }),
            step_count: 0,
          },
        ]),
      ),
      http.post("*/api/staging/:id/undo", ({ params }) => {
        posted.push({ url: `/undo/${params.id}`, method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /Full Brain/i }));
    await user.type(screen.getByPlaceholderText(/full tool access/i), "make a note");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    // The persisted applied chip with its Undo button renders after the re-sync.
    await screen.findByText(/Created note Foo/i);
    await user.click(screen.getByRole("button", { name: /^Undo$/i }));

    await waitFor(() =>
      expect(posted.some((p) => p.method === "POST" && /\/undo\/5/.test(p.url))).toBe(true),
    );
    // The chip flips to the "undone" affordance.
    await screen.findByText(/undone/i);
  });
});

// ---------------------------------------------------------------------------- //
// (f) External-lookup proposal — the medical_reference gate
// ---------------------------------------------------------------------------- //
describe("Chat — external-lookup gate", () => {
  it("approving a proposal posts the edited term and auto-continues the turn", async () => {
    let turn = 0;
    streamImpl = async (_a, onEvent) => {
      turn++;
      if (turn === 1) onEvent({ type: "external_proposal", id: 11, term: "metformin" });
      else onEvent({ type: "token", text: "Per MedlinePlus…" });
    };
    server.use(
      http.post("*/api/external-lookups/:id/approve", async ({ request, params }) => {
        posted.push({ url: `/approve/${params.id}`, method: "POST", body: await request.json() });
        return HttpResponse.json({ ok: true, found: true, term: "metformin" });
      }),
    );
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /Full Brain/i }));
    await user.type(screen.getByPlaceholderText(/full tool access/i), "what is metformin");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    // The inline confirm chip appears with an editable term.
    const termBox = await screen.findByLabelText(/External search term/i);
    expect((termBox as HTMLInputElement).value).toBe("metformin");

    await user.click(screen.getByRole("button", { name: /Approve & search/i }));

    // The exact term is sent to the approve endpoint and a continuation turn streams.
    await waitFor(() =>
      expect(posted.some((p) => /\/approve\/11/.test(p.url))).toBe(true),
    );
    expect(posted.find((p) => /\/approve\/11/.test(p.url))!.body.term).toBe("metformin");
    await waitFor(() => expect(streamCalls.length).toBeGreaterThanOrEqual(2));
    await screen.findByText(/Per MedlinePlus/i);
  });

  it("skipping a proposal denies the lookup and continues without it", async () => {
    let turn = 0;
    streamImpl = async (_a, onEvent) => {
      turn++;
      if (turn === 1) onEvent({ type: "external_proposal", id: 22, term: "aspirin" });
      else onEvent({ type: "token", text: "Using my own notes." });
    };
    server.use(
      http.post("*/api/external-lookups/:id/deny", ({ params }) => {
        posted.push({ url: `/deny/${params.id}`, method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<Chat />);
    await user.click(screen.getByRole("button", { name: /Full Brain/i }));
    await user.type(screen.getByPlaceholderText(/full tool access/i), "tell me about aspirin");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await screen.findByLabelText(/External search term/i);
    await user.click(screen.getByRole("button", { name: /^Skip$/i }));

    await waitFor(() =>
      expect(posted.some((p) => /\/deny\/22/.test(p.url))).toBe(true),
    );
    await waitFor(() => expect(streamCalls.length).toBeGreaterThanOrEqual(2));
    await screen.findByText(/Using my own notes/i);
  });
});
