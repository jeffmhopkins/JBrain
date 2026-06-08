// Component-integration (Tier 2) tests for Attachments.tsx — the per-note attachment
// surface on a note / compose box. It lists a note's attachments, uploads new files
// (XHR with progress, via the `uploadAttachment` api helper), previews images inline,
// streams audio/video into an inline <audio>/<video> element, kicks AI vision analysis
// (images) or local transcription (audio/video) with polling, and deletes.
//
// Network: every route the panel touches goes through MSW (setup.ts runs
// onUnhandledRequest:"error", so each one is stubbed here):
//   GET    /api/notes/:slug/attachments          — the attachment list
//   GET    /api/attachments/:id/media-url         — short-lived streaming URL (audio/video)
//   GET    /api/attachments/:id/download          — image bytes (attachmentImageUrl sniffs them)
//   POST   /api/attachments/:id/analyze           — kick AI vision (image)
//   POST   /api/attachments/:id/transcribe        — kick local transcription (audio/video)
//   GET    /api/attachments/:id/analysis-status   — poll after a kick
//   DELETE /api/attachments/:id                   — remove
// The XHR-based `uploadAttachment` is mocked directly (vi.mock("../api") keeping every
// other export real) — MSW can't observe XHR uploads and we want to assert the call +
// drive the post-upload reload. The llm/transcription capability gates read the health
// store, seeded ready via ingestVerify (mirrors LabImportPanel.test.tsx). No production
// code is touched.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, within, cleanup, fireEvent } from "../test/render";
import { server } from "../test/server";
import { __reset, ingestVerify } from "../health";

// Attachments doesn't import useAuth, but a sibling (Modal/Icon) could pull the App tree
// in transitively; stub ../App defensively so the auth/router App tree never loads.
vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

// Mock ONLY uploadAttachment (XHR — invisible to MSW); everything else stays real so the
// component's get/post/del/attachmentImageUrl/attachmentMediaUrl hit the MSW server.
vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return { ...actual, uploadAttachment: vi.fn() };
});

import * as api from "../api";
import Attachments from "./Attachments";

const READY_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: false } },
  embeddings: { state: "ready" }, transcription: { state: "ready" },
  push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
};

const SLUG = "trip-notes";

// A tiny valid PNG header so attachmentImageUrl's magic-byte sniff passes for image tests.
const PNG_BYTES = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]);

type Att = {
  id: number; filename: string; mime: string; byte_size: number; created_at: string;
  analysis_status?: string | null; analysis_md?: string | null; analysis_detail?: string | null;
};

function att(over: Partial<Att> = {}): Att {
  return {
    id: 1, filename: "readme.txt", mime: "text/plain", byte_size: 2048,
    created_at: "2026-06-01T00:00:00Z", analysis_status: "none", analysis_md: null,
    ...over,
  };
}

// --- recording handlers ----------------------------------------------------- //
let posted: { url: string; body: any }[] = [];
let deleted: string[] = [];

// Register the list GET (overridable, with a mutable cell so a delete/upload can change it)
// plus the media-url / download / analyze / transcribe / status / delete routes.
function mountHandlers(initial: Att[]) {
  const state = { items: [...initial] };
  server.use(
    http.get(`*/api/notes/${SLUG}/attachments`, () => HttpResponse.json(state.items)),
    http.get(`*/api/attachments/:id/media-url`, ({ params }) =>
      HttpResponse.json({ url: `/api/attachments/${params.id}/stream` })),
    http.get(`*/api/attachments/:id/download`, () =>
      new HttpResponse(PNG_BYTES, { headers: { "Content-Type": "image/png" } })),
    http.post(`*/api/attachments/:id/analyze`, async ({ request, params }) => {
      posted.push({ url: request.url, body: await request.json().catch(() => null) });
      const a = state.items.find((x) => String(x.id) === String(params.id));
      if (a) a.analysis_status = "pending";
      return HttpResponse.json({ status: "pending" });
    }),
    http.post(`*/api/attachments/:id/transcribe`, async ({ request, params }) => {
      posted.push({ url: request.url, body: await request.json().catch(() => null) });
      const a = state.items.find((x) => String(x.id) === String(params.id));
      if (a) a.analysis_status = "pending";
      return HttpResponse.json({ status: "pending" });
    }),
    http.get(`*/api/attachments/:id/analysis-status`, () => HttpResponse.json({ status: "done" })),
    http.delete(`*/api/attachments/:id`, ({ params }) => {
      deleted.push(String(params.id));
      state.items = state.items.filter((x) => String(x.id) !== String(params.id));
      return HttpResponse.json({ ok: true });
    }),
  );
  return state;
}

const analyzeCalls = () => posted.filter((p) => p.url.includes("/analyze"));
const transcribeCalls = () => posted.filter((p) => p.url.includes("/transcribe"));

beforeEach(() => {
  __reset();
  ingestVerify({ capabilities: READY_CAPS });   // llm + transcription ready → enrich buttons enabled
  posted = [];
  deleted = [];
  // Inline players/previews mint object URLs; jsdom has no implementation, so
  // define the methods before stubbing (vi.spyOn needs an existing property).
  (URL as any).createObjectURL = vi.fn(() => "blob:mock");
  (URL as any).revokeObjectURL = vi.fn();
  (api.uploadAttachment as any).mockReset();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

// ---------------------------------------------------------------------------- //
// (a) listing + type badges/affordances
// ---------------------------------------------------------------------------- //
describe("Attachments — listing", () => {
  it("renders the attachment list with size and per-type action buttons", async () => {
    mountHandlers([
      att({ id: 1, filename: "readme.txt", mime: "text/plain", byte_size: 2048 }),
      att({ id: 2, filename: "photo.png", mime: "image/png", byte_size: 5000 }),
      att({ id: 3, filename: "memo.m4a", mime: "audio/mp4", byte_size: 1500000 }),
    ]);
    renderWithProviders(<Attachments slug={SLUG} />);

    expect(await screen.findByText("readme.txt")).toBeInTheDocument();
    expect(screen.getByText("photo.png")).toBeInTheDocument();
    expect(screen.getByText("memo.m4a")).toBeInTheDocument();

    // Human-readable sizes (B / KB / MB) — a quick "type-ish" badge of scale.
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getByText("1.4 MB")).toBeInTheDocument();

    // The image gets an "Analyze with AI" affordance; audio gets "Transcribe".
    expect(await screen.findByRole("button", { name: /Analyze with AI/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Transcribe$/i })).toBeInTheDocument();
    // The plain text file gets neither enrichment button.
    expect(screen.getAllByRole("button", { name: /^Delete$/i })).toHaveLength(3);
  });

  it("renders the empty state (no list items, just the attach control)", async () => {
    mountHandlers([]);
    renderWithProviders(<Attachments slug={SLUG} />);

    expect(await screen.findByRole("button", { name: /\+ Attach file/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryAllByRole("button", { name: /^Delete$/i })).toHaveLength(0));
  });
});

// ---------------------------------------------------------------------------- //
// (b) inline audio/video players
// ---------------------------------------------------------------------------- //
describe("Attachments — inline media", () => {
  it("streams audio into an inline <audio> element via the media-url endpoint", async () => {
    mountHandlers([att({ id: 7, filename: "voice.mp3", mime: "audio/mpeg", byte_size: 900000 })]);
    const { container } = renderWithProviders(<Attachments slug={SLUG} />);

    await screen.findByText("voice.mp3");
    await waitFor(() => {
      const audio = container.querySelector("audio");
      expect(audio).toBeTruthy();
      expect(audio?.getAttribute("src")).toContain("/api/attachments/7/stream");
    });
    expect(container.querySelector("video")).toBeNull();
  });

  it("streams video into an inline <video> element", async () => {
    mountHandlers([att({ id: 8, filename: "clip.mp4", mime: "video/mp4", byte_size: 9000000 })]);
    const { container } = renderWithProviders(<Attachments slug={SLUG} />);

    await screen.findByText("clip.mp4");
    await waitFor(() => {
      const video = container.querySelector("video");
      expect(video).toBeTruthy();
      expect(video?.getAttribute("src")).toContain("/api/attachments/8/stream");
    });
  });
});

// ---------------------------------------------------------------------------- //
// (c) upload
// ---------------------------------------------------------------------------- //
describe("Attachments — upload", () => {
  it("uploading a file calls uploadAttachment and reflects the new attachment", async () => {
    const state = mountHandlers([]);
    // The api mock simulates the server: report progress, persist the new row, resolve.
    (api.uploadAttachment as any).mockImplementation(
      async (slug: string, file: File, onProgress?: (p: number) => void) => {
        onProgress?.(100);
        state.items.push(att({ id: 99, filename: file.name, mime: file.type, byte_size: file.size }));
        return { id: 99, analysis: { status: "none" } };
      },
    );

    const { container } = renderWithProviders(<Attachments slug={SLUG} />);
    await screen.findByRole("button", { name: /\+ Attach file/i });

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["hello"], "newdoc.txt", { type: "text/plain" });
    // userEvent.upload can be a no-op for display:none inputs; drive change directly.
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    fireEvent.change(input);

    await waitFor(() => expect(api.uploadAttachment as any).toHaveBeenCalledTimes(1));
    expect((api.uploadAttachment as any).mock.calls[0][0]).toBe(SLUG);
    expect((api.uploadAttachment as any).mock.calls[0][1]).toBe(file);

    // The post-upload reload shows the new attachment row.
    expect(await screen.findByText("newdoc.txt")).toBeInTheDocument();
  });

  it("surfaces an upload error without adding a row", async () => {
    mountHandlers([]);
    (api.uploadAttachment as any).mockRejectedValue(new Error("upload failed"));

    const { container } = renderWithProviders(<Attachments slug={SLUG} />);
    await screen.findByRole("button", { name: /\+ Attach file/i });

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["x"], "bad.bin", { type: "application/octet-stream" });
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    fireEvent.change(input);

    expect(await screen.findByText(/bad\.bin: upload failed/i)).toBeInTheDocument();
    expect(screen.queryByText("bad.bin")).not.toBeInTheDocument();   // no list row added
  });
});

// ---------------------------------------------------------------------------- //
// (d) AI analyze / transcribe (kick + assert request)
// ---------------------------------------------------------------------------- //
describe("Attachments — enrich", () => {
  it("Analyze with AI posts to /analyze and shows the analyzing state", async () => {
    mountHandlers([att({ id: 11, filename: "chart.png", mime: "image/png", byte_size: 4000 })]);
    const { user } = renderWithProviders(<Attachments slug={SLUG} />);

    const btn = await screen.findByRole("button", { name: /Analyze with AI/i });
    await user.click(btn);

    await waitFor(() => expect(analyzeCalls()).toHaveLength(1));
    expect(analyzeCalls()[0].url).toContain("/api/attachments/11/analyze");
    expect(analyzeCalls()[0].body).toEqual({ force: false });
    // Optimistic "pending" → the analyzing-image notice.
    expect(await screen.findByText(/Analyzing image/i)).toBeInTheDocument();
  });

  it("Transcribe posts to /transcribe for an audio attachment", async () => {
    mountHandlers([att({ id: 12, filename: "memo.wav", mime: "audio/wav", byte_size: 30000 })]);
    const { user } = renderWithProviders(<Attachments slug={SLUG} />);

    const btn = await screen.findByRole("button", { name: /^Transcribe$/i });
    await user.click(btn);

    await waitFor(() => expect(transcribeCalls()).toHaveLength(1));
    expect(transcribeCalls()[0].url).toContain("/api/attachments/12/transcribe");
    expect(await screen.findByText(/Transcribing/i)).toBeInTheDocument();
  });

  it("shows an existing AI summary sidecar (analysis_md) collapsed under the row", async () => {
    mountHandlers([att({
      id: 13, filename: "graph.png", mime: "image/png", byte_size: 4000,
      analysis_status: "done", analysis_md: "A bar chart of monthly spend.",
    })]);
    renderWithProviders(<Attachments slug={SLUG} />);

    const details = (await screen.findByText(/AI summary/i)).closest("details") as HTMLElement;
    expect(within(details).getByText(/bar chart of monthly spend/i)).toBeInTheDocument();
    // A finished analysis offers a re-run.
    expect(await screen.findByRole("button", { name: /Re-analyze/i })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (e) delete
// ---------------------------------------------------------------------------- //
describe("Attachments — delete", () => {
  it("Delete (confirmed) DELETEs the attachment and removes its row", async () => {
    mountHandlers([
      att({ id: 21, filename: "keep.txt", mime: "text/plain", byte_size: 100 }),
      att({ id: 22, filename: "gone.txt", mime: "text/plain", byte_size: 100 }),
    ]);
    const onChanged = vi.fn();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = renderWithProviders(<Attachments slug={SLUG} onNoteChanged={onChanged} />);

    await screen.findByText("gone.txt");
    // Delete the second row.
    const rows = screen.getAllByRole("button", { name: /^Delete$/i });
    await user.click(rows[1]);

    await waitFor(() => expect(deleted).toEqual(["22"]));
    await waitFor(() => expect(screen.queryByText("gone.txt")).not.toBeInTheDocument());
    expect(screen.getByText("keep.txt")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
  });

  it("Delete is a no-op when the confirm is dismissed", async () => {
    mountHandlers([att({ id: 31, filename: "stay.txt", mime: "text/plain", byte_size: 100 })]);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { user } = renderWithProviders(<Attachments slug={SLUG} />);

    await user.click(await screen.findByRole("button", { name: /^Delete$/i }));
    expect(deleted).toEqual([]);
    expect(screen.getByText("stay.txt")).toBeInTheDocument();
  });
});
