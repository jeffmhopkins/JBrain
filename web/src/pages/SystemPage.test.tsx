// Component-integration (Tier 2) tests for SystemPage — the system/settings surface:
// version + live server-update trigger (with the post-trigger health poll), the LLM
// model picker (ModelPicker), media/transcription settings (MediaSettings), the
// note-analysis master switch (AutoAnalyzeSetting), and the owner name (OwnerSetting).
//
// Network: MSW only (setup.ts runs onUnhandledRequest:"error"), so EVERY endpoint the
// page + its child cards touch on mount and per flow is stubbed via server.use(...):
//   GET  /api/system/version, /api/system/stats        (SystemPage effects)
//   GET  /api/prompts            PUT /api/prompts/:key  (ModelPicker)
//   GET  /api/system/settings/media   PUT  …            (MediaSettings)
//   GET  /api/system/settings/auto-analyze  PUT  …      (AutoAnalyzeSetting)
//   GET  /api/people/owner       PUT  /api/people/owner (OwnerSetting)
//   POST /api/system/update      GET  /api/health       (update trigger + poll)
//
// useAuth() is stubbed (../App) so we don't drag in the auth/router tree — we supply
// the exact fields SystemPage reads (versions, vapid key, disconnect). UpdateConsole is
// mocked to a stub: it self-polls /api/system/update-log on a 1.2s loop, which is its
// own unit's concern and would otherwise need mocking + introduce a live timer; the
// "Update server" click opens it, so stubbing it keeps these tests deterministic.
// The health store is seeded ready so ModelPicker's useCapability("llm") shows enabled.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import { __reset, ingestVerify } from "../health";

const READY_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: true } },
  embeddings: { state: "ready" }, transcription: { state: "ready" },
  push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
};

// SystemPage reads useAuth() for the version banner, the VAPID key (push test), and
// disconnect. Supply exactly those fields so we needn't mount the real provider.
const disconnect = vi.fn();
vi.mock("../App", () => ({
  useAuth: () => ({
    disconnect,
    pwaVersion: "9.9.9",
    serverVersion: "9.9.9",
    versionMismatch: false,
    vapidPublicKey: "",
  }),
  PWA_VERSION: "test",
}));

// UpdateConsole self-polls /api/system/update-log (+ a Caddy fallback) on a live timer.
// Stub it so opening it (via "Update server"/"Live console") stays deterministic.
vi.mock("../components/UpdateConsole", () => ({
  default: ({ onClose }: { onClose: () => void }) => (
    <div data-testid="update-console">
      <button onClick={onClose}>close console</button>
    </div>
  ),
}));

import SystemPage from "./SystemPage";

// /api/system/stats payload (drives the Maintenance card).
const STATS = {
  storage: { percent: 42, free: 5e9, total: 1e10, db_bytes: 2e6, attachments_count: 3, tiles_bytes: 0 },
  uptime_seconds: 3700, started_at: "2026-06-01T00:00:00",
  daily_warn_usd: 5,
  tokens: { today: { cost: 0.12, input: 1000, output: 500, calls: 4 }, month: { cost: 1.5, input: 2e5, output: 1e5 }, tz: "UTC" },
};

// /api/prompts rows ModelPicker reads (only the tier keys matter).
const PROMPTS = [
  { key: "agent.model", effective: "" },
  { key: "models.cheap", effective: "" },
  { key: "models.synthesis", effective: "" },
  { key: "models.vision", effective: "" },
  { key: "models.default", effective: "" },
];

const MEDIA = {
  audio_model: "tiny", audio_compute_type: "int8",
  video_frame_interval: "30s", video_frame_max: 8,
  audio_model_options: ["tiny", "base", "small"],
  compute_type_options: ["int8", "float16"],
};

// Register the happy-path GETs every mount fires across SystemPage + its child cards.
// `latest` lets a test flip what /api/system/version returns (no-update vs update).
function mountHandlers(opts: { version?: any } = {}) {
  const version = opts.version ?? { current: "9.9.9", update_available: false };
  server.use(
    http.get("*/api/system/version", () => HttpResponse.json(version)),
    http.get("*/api/system/stats", () => HttpResponse.json(STATS)),
    http.get("*/api/prompts", () => HttpResponse.json(PROMPTS)),
    http.get("*/api/system/settings/media", () => HttpResponse.json(MEDIA)),
    http.get("*/api/system/settings/auto-analyze", () => HttpResponse.json({ enabled: false })),
    http.get("*/api/people/owner", () =>
      HttpResponse.json({ id: 1, name: "Jeff", display: "Jeff", aliases: "", is_set: true })),
  );
}

beforeEach(() => {
  __reset();
  // Seed the health store ready so ModelPicker's useCapability("llm") renders enabled
  // (and won't show a spurious missing-provider-key warning).
  ingestVerify({ capabilities: READY_CAPS });
  disconnect.mockClear();
});
afterEach(() => { vi.restoreAllMocks(); });

describe("SystemPage", () => {
  it("loads and renders the version + maintenance + settings cards", async () => {
    mountHandlers();
    renderWithProviders(<SystemPage />, { route: "/system" });

    // Version banner from useAuth + the "latest release" line from /api/system/version.
    expect(screen.getByText(/App v9\.9\.9/)).toBeInTheDocument();
    expect(await screen.findByText(/You.re on the latest server release/)).toBeInTheDocument();

    // Maintenance card renders only after /api/system/stats resolves.
    expect(await screen.findByText("Maintenance")).toBeInTheDocument();
    expect(screen.getByText("Uptime")).toBeInTheDocument();

    // Child setting cards render once their GETs resolve.
    expect(await screen.findByText("Media & transcription")).toBeInTheDocument();
    expect(await screen.findByText("Note analysis")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Owner" })).toBeInTheDocument();
    expect(screen.getByText("Model")).toBeInTheDocument();
  });

  it("changing the LLM model for a tier PUTs /api/prompts/<key> with the chosen value", async () => {
    mountHandlers();
    let putKey: string | null = null;
    let putBody: any = null;
    server.use(
      http.put("*/api/prompts/:key", async ({ params, request }) => {
        putKey = params.key as string;
        putBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    // The "Chat agent" tier select (label text identifies its row).
    const row = (await screen.findByText("Chat agent")).closest("label")!;
    const select = within(row as HTMLElement).getByRole("combobox");
    await user.selectOptions(select, "claude-opus-4-8");

    await waitFor(() => expect(putKey).toBe("agent.model"));
    expect(putBody).toEqual({ value: "claude-opus-4-8" });
  });

  it("saving media settings PUTs the edited body and shows the saved confirmation", async () => {
    mountHandlers();
    let putBody: any = null;
    server.use(
      http.put("*/api/system/settings/media", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json({ ...MEDIA, ...putBody });
      }),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    const mediaCard = (await screen.findByText("Media & transcription")).closest(".card")!;
    const scoped = within(mediaCard as HTMLElement);

    // Change the Whisper model select, then Save.
    const modelSelect = scoped.getAllByRole("combobox")[0];
    await user.selectOptions(modelSelect, "small");
    await user.click(scoped.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putBody).toEqual({
      audio_model: "small", audio_compute_type: "int8",
      video_frame_interval: "30s", video_frame_max: 8,
    });
    // Saved feedback (from setMediaSettings success).
    expect(await scoped.findByText(/new transcriptions use these right away/)).toBeInTheDocument();
  });

  it("surfaces an error message when saving media settings fails", async () => {
    mountHandlers();
    server.use(
      http.put("*/api/system/settings/media", () =>
        HttpResponse.json({ detail: "Disk full" }, { status: 500 })),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    const mediaCard = (await screen.findByText("Media & transcription")).closest(".card")!;
    const scoped = within(mediaCard as HTMLElement);
    await user.click(scoped.getByRole("button", { name: "Save" }));

    expect(await scoped.findByText("Disk full")).toBeInTheDocument();
  });

  it("toggling note-analysis PUTs the new flag and reflects the saved state", async () => {
    mountHandlers();
    let putBody: any = null;
    server.use(
      http.put("*/api/system/settings/auto-analyze", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json({ enabled: true });
      }),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    const card = (await screen.findByText("Note analysis")).closest(".card")!;
    const scoped = within(card as HTMLElement);
    await user.click(scoped.getByRole("checkbox"));

    await waitFor(() => expect(putBody).toEqual({ enabled: true }));
    expect(await scoped.findByText(/new notes and their attachments are processed/)).toBeInTheDocument();
  });

  it("saving the owner name PUTs /api/people/owner and confirms", async () => {
    mountHandlers();
    let putBody: any = null;
    server.use(
      http.put("*/api/people/owner", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json({ id: 1, name: "Jeff Hopkins", display: "Jeff Hopkins", aliases: "", is_set: true });
      }),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    const card = (await screen.findByRole("heading", { name: "Owner" })).closest(".card")!;
    const scoped = within(card as HTMLElement);
    const nameInput = scoped.getByPlaceholderText(/Jeff Hopkins/) as HTMLInputElement;
    await user.clear(nameInput);
    await user.type(nameInput, "Jeff Hopkins");
    await user.click(scoped.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBody).toEqual({ name: "Jeff Hopkins", aliases: "" }));
    expect(await scoped.findByText(/Re-analyze your notes/)).toBeInTheDocument();
  });

  it("triggers the server update (POST /api/system/update) and starts the restart poll", async () => {
    mountHandlers({ version: { current: "9.9.9", latest: "9.9.10", update_available: true } });
    let updateHit = false;
    server.use(
      http.post("*/api/system/update", () => {
        updateHit = true;
        // A configured deploy command "started" → the page enters the restart-poll path.
        return HttpResponse.json({ started: true });
      }),
      http.get("*/api/health", () => new HttpResponse(null, { status: 200 })),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    // The update-available row offers "Update server".
    const trigger = await screen.findByRole("button", { name: /Update server/ });
    await user.click(trigger);

    await waitFor(() => expect(updateHit).toBe(true));
    // The status line moves into the "waiting for the server to restart" phase, and the
    // mocked UpdateConsole opens (the click does setConsole(true) before doUpdate()).
    expect(await screen.findByText(/waiting for the server to restart/)).toBeInTheDocument();
    expect(screen.getByTestId("update-console")).toBeInTheDocument();
  });

  it("shows an error if the update request can't reach the server", async () => {
    mountHandlers({ version: { current: "9.9.9", latest: "9.9.10", update_available: true } });
    server.use(
      http.post("*/api/system/update", () => HttpResponse.error()),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    await user.click(await screen.findByRole("button", { name: /Update server/ }));

    expect(await screen.findByText(/Couldn.t reach the server to start the update/)).toBeInTheDocument();
  });

  it("Disconnect calls the auth disconnect()", async () => {
    mountHandlers();
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    await user.click(await screen.findByRole("button", { name: "Disconnect" }));
    expect(disconnect).toHaveBeenCalledTimes(1);
  });
});
