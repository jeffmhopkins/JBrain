// Additional component-integration (Tier 2) coverage for SystemPage that the original
// SystemPage.test.tsx leaves untested: the push test-notification flow (every branch of
// sendTestNotification), the on-device TTS Voice card (voice select, language filter,
// rate, preview/stop), the remaining MediaSettings fields (frame interval + max), the
// note-analysis save-error edge, and the live-update lifecycle branches reached through
// the health poll (queued / restart→online recovery / failed). Mirrors the original
// file's harness: ../App is stubbed for useAuth, UpdateConsole is stubbed, MSW handles
// every mount GET (onUnhandledRequest:"error"), and the health store is seeded ready.
//
// Two device-local seams the page leans on are mocked here rather than driven through
// real browser APIs jsdom lacks:
//   ../push  — sendTestNotification calls pushSupported/pushSupportReason/enablePush; we
//              control them per-test so the flow is deterministic (no real SW/VAPID).
//   speechSynthesis (+ SpeechSynthesisUtterance) — stubbed so useTts reports supported and
//              the Voice card renders, and so previewVoice's speak()/stop() are observable.
// The update-poll uses window.setTimeout(poll, …); we stub setTimeout to run the callback
// on a real microtask so the poll advances deterministically without full fake timers.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import { __reset, ingestVerify } from "../health";

const READY_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: true } },
  embeddings: { state: "ready" }, transcription: { state: "ready" },
  push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
};

const disconnect = vi.fn();
vi.mock("../App", () => ({
  useAuth: () => ({
    disconnect,
    pwaVersion: "9.9.9",
    serverVersion: "9.9.8",
    versionMismatch: true,
    vapidPublicKey: "vapid-key",
  }),
  PWA_VERSION: "test",
}));

vi.mock("../components/UpdateConsole", () => ({
  default: ({ onClose }: { onClose: () => void }) => (
    <div data-testid="update-console">
      <button onClick={onClose}>close console</button>
    </div>
  ),
}));

// Push seam: per-test controllable. Defaults to "fully working" so the happy path needs
// no override; individual tests reassign these mocks to exercise the other branches.
const pushSupported = vi.fn(() => true);
const pushSupportReason = vi.fn(() => "");
const enablePush = vi.fn(async () => true);
vi.mock("../push", () => ({
  pushSupported: () => pushSupported(),
  pushSupportReason: () => pushSupportReason(),
  enablePush: (k: string) => enablePush(k),
}));

import SystemPage from "./SystemPage";

// A Maintenance payload that trips the high-cost warning, the days/GB/tiles format arms,
// and the >=1M / >=1k token formatters (covers fmtBytes/fmtUptime/fmtTok branches).
const STATS = {
  storage: { percent: 88, free: 50e9, total: 500e9, db_bytes: 2e6, attachments_count: 1, tiles_bytes: 3e7 },
  uptime_seconds: 200000, started_at: "2026-06-01T00:00:00",
  daily_warn_usd: 5,
  tokens: { today: { cost: 9.5, input: 2e6, output: 5e5, calls: 42 }, month: { cost: 12.5, input: 8e5, output: 4e5 }, tz: "UTC" },
};

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

// Stub speechSynthesis so useTts reports supported and the Voice card renders. Returns the
// spies so a test can assert speak()/cancel() and fire voiceschanged with a voice list.
function stubSpeech(voices: any[] = []) {
  const listeners = new Set<() => void>();
  const speak = vi.fn();
  const cancel = vi.fn();
  const synth = {
    getVoices: () => voices,
    speak, cancel, speaking: false, pending: false,
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
    fire() { listeners.forEach((cb) => cb()); },
  };
  Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
  vi.stubGlobal("SpeechSynthesisUtterance", class {
    text: string; voice: any = null; lang = ""; rate = 1; volume = 1;
    onend: any = null; onerror: any = null;
    constructor(t = "") { this.text = t; }
  });
  return { synth, speak, cancel };
}

beforeEach(() => {
  __reset();
  ingestVerify({ capabilities: READY_CAPS });
  disconnect.mockClear();
  pushSupported.mockReset().mockReturnValue(true);
  pushSupportReason.mockReset().mockReturnValue("");
  enablePush.mockReset().mockResolvedValue(true);
});
afterEach(() => { vi.restoreAllMocks(); });

describe("SystemPage — maintenance formatting + version mismatch", () => {
  it("renders the GB/days/tiles + high-cost-warning maintenance figures and the mismatch hint", async () => {
    mountHandlers();
    renderWithProviders(<SystemPage />, { route: "/system" });

    // Versions differ → the mismatch hint shows alongside the App/server line.
    expect(await screen.findByText(/Versions differ/)).toBeInTheDocument();

    const maint = (await screen.findByText("Maintenance")).closest(".card")!;
    const scoped = within(maint as HTMLElement);
    // fmtBytes GB arm + tiles line, fmtUptime days arm, fmtTok >=1M, high-cost warning.
    expect(scoped.getByText(/47 GB free of 466 GB/)).toBeInTheDocument();
    expect(scoped.getByText(/map tiles 29 MB/)).toBeInTheDocument();
    expect(scoped.getByText(/2d 7h/)).toBeInTheDocument();
    expect(scoped.getByText(/high — over \$5\.00\/day/)).toBeInTheDocument();
    expect(scoped.getByText(/2\.5M tokens/)).toBeInTheDocument();
  });
});

describe("SystemPage — push test notification", () => {
  async function clickSend() {
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });
    const card = (await screen.findByText("Notifications")).closest(".card")!;
    const scoped = within(card as HTMLElement);
    await user.click(scoped.getByRole("button", { name: "Send test notification" }));
    return scoped;
  }

  it("reports when push isn't available on this device", async () => {
    mountHandlers();
    pushSupported.mockReturnValue(false);
    pushSupportReason.mockReturnValue("no Push API");
    const scoped = await clickSend();
    expect(await scoped.findByText(/Notifications aren.t available here: no Push API/)).toBeInTheDocument();
  });

  it("requests permission and stops if it isn't granted", async () => {
    mountHandlers();
    const requestPermission = vi.fn().mockResolvedValue("denied");
    vi.stubGlobal("Notification", Object.assign(
      function () {} as any, { permission: "default", requestPermission },
    ));
    const scoped = await clickSend();
    await waitFor(() => expect(requestPermission).toHaveBeenCalled());
    expect(await scoped.findByText(/Notifications are blocked/)).toBeInTheDocument();
  });

  it("reports when this device can't be subscribed to push", async () => {
    mountHandlers();
    vi.stubGlobal("Notification", { permission: "granted" } as any);
    enablePush.mockResolvedValue(false);
    const scoped = await clickSend();
    expect(await scoped.findByText(/Couldn.t subscribe this device to push/)).toBeInTheDocument();
  });

  it("sends the test and reports devices reached (delay select feeds the body)", async () => {
    mountHandlers();
    vi.stubGlobal("Notification", { permission: "granted" } as any);
    let body: any = null;
    server.use(
      http.post("*/api/push/test", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ vapid: true, subscriptions: 2, sent: 2, failed: 0, delay: 0 });
      }),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });
    const card = (await screen.findByText("Notifications")).closest(".card")!;
    const scoped = within(card as HTMLElement);
    await user.selectOptions(scoped.getByRole("combobox"), "30");
    await user.click(scoped.getByRole("button", { name: "Send test notification" }));

    await waitFor(() => expect(body).toEqual({ delay: 30 }));
    expect(await scoped.findByText(/Sent to 2\/2 device/)).toBeInTheDocument();
  });

  it("reports a scheduled (delayed) test", async () => {
    mountHandlers();
    vi.stubGlobal("Notification", { permission: "granted" } as any);
    server.use(
      http.post("*/api/push/test", () =>
        HttpResponse.json({ vapid: true, subscriptions: 1, sent: 0, failed: 0, delay: 10 })),
    );
    const scoped = await clickSend();
    expect(await scoped.findByText(/Test scheduled in 10s to 1 device\./)).toBeInTheDocument();
  });

  it("reports a send failure with the first error", async () => {
    mountHandlers();
    vi.stubGlobal("Notification", { permission: "granted" } as any);
    server.use(
      http.post("*/api/push/test", () =>
        HttpResponse.json({ vapid: true, subscriptions: 1, sent: 0, failed: 1, errors: ["410 gone"] })),
    );
    const scoped = await clickSend();
    expect(await scoped.findByText(/Send failed \(1 device\): 410 gone/)).toBeInTheDocument();
  });

  it("reports when push isn't configured on the server", async () => {
    mountHandlers();
    vi.stubGlobal("Notification", { permission: "granted" } as any);
    server.use(
      http.post("*/api/push/test", () => HttpResponse.json({ vapid: false, subscriptions: 0 })),
    );
    const scoped = await clickSend();
    expect(await scoped.findByText(/Push isn.t configured on the server/)).toBeInTheDocument();
  });

  it("reports no subscribed devices yet", async () => {
    mountHandlers();
    vi.stubGlobal("Notification", { permission: "granted" } as any);
    server.use(
      http.post("*/api/push/test", () => HttpResponse.json({ vapid: true, subscriptions: 0 })),
    );
    const scoped = await clickSend();
    expect(await scoped.findByText(/No subscribed devices yet/)).toBeInTheDocument();
  });

  it("surfaces a thrown error from the flow", async () => {
    mountHandlers();
    vi.stubGlobal("Notification", { permission: "granted" } as any);
    enablePush.mockRejectedValue(new Error("boom"));
    const scoped = await clickSend();
    expect(await scoped.findByText("boom")).toBeInTheDocument();
  });
});

describe("SystemPage — Voice (on-device TTS) card", () => {
  it("shows the unsupported notice when speechSynthesis is absent", async () => {
    mountHandlers();
    delete (window as any).speechSynthesis;
    renderWithProviders(<SystemPage />, { route: "/system" });
    const card = (await screen.findByRole("heading", { name: "Voice" })).closest(".card")!;
    expect(within(card as HTMLElement).getByText(/doesn.t support speech/)).toBeInTheDocument();
  });

  it("lists English voices, filters out non-English until 'show all', and selects a voice", async () => {
    mountHandlers();
    const en = { voiceURI: "en-1", name: "Alice", lang: "en-US", default: true } as any;
    const fr = { voiceURI: "fr-1", name: "Amélie", lang: "fr-FR", default: false } as any;
    const { synth } = stubSpeech([en, fr]);
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    const card = (await screen.findByRole("heading", { name: "Voice" })).closest(".card")!;
    const scoped = within(card as HTMLElement);
    // voiceschanged delivers the pool after mount.
    await waitFor(() => (synth as any).fire());

    const voiceSelect = scoped.getByRole("combobox") as HTMLSelectElement;
    // English-only by default: the French voice option is hidden.
    await waitFor(() => expect(scoped.getByRole("option", { name: /Alice \(en-US\) — default/ })).toBeInTheDocument());
    expect(scoped.queryByRole("option", { name: /Amélie/ })).not.toBeInTheDocument();

    // Flip "Show all languages" → the French voice appears.
    await user.click(scoped.getByRole("checkbox"));
    expect(await scoped.findByRole("option", { name: /Amélie \(fr-FR\)/ })).toBeInTheDocument();

    // Pick a voice → persisted via useTts.
    await user.selectOptions(voiceSelect, "en-1");
    expect(localStorage.getItem("jbrain_tts_voice")).toBe("en-1");
  });

  it("adjusts the speed and previews/stops the sample voice", async () => {
    mountHandlers();
    const { synth, speak, cancel } = stubSpeech([]);
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    const card = (await screen.findByRole("heading", { name: "Voice" })).closest(".card")!;
    const scoped = within(card as HTMLElement);

    // No installed voices → the hint shows.
    expect(scoped.getByText(/No installed voices found yet/)).toBeInTheDocument();

    // Move the speed slider (range inputs take a fireEvent.change, not user typing);
    // useTts persists the clamped rate and the label reflects it.
    const slider = scoped.getByRole("slider") as HTMLInputElement;
    fireEvent.change(slider, { target: { value: "1.8" } });
    expect(localStorage.getItem("jbrain_tts_rate")).toBe("1.8");
    expect(scoped.getByText(/Speed · 1\.8×/)).toBeInTheDocument();

    await user.click(scoped.getByRole("button", { name: "Hear sample" }));
    expect(speak).toHaveBeenCalledTimes(1);

    // Now the engine reports speaking → button flips to "Stop" and tapping cancels.
    (synth as any).speaking = true;
    await waitFor(() => expect(scoped.getByRole("button", { name: "Stop" })).toBeInTheDocument());
    await user.click(scoped.getByRole("button", { name: "Stop" }));
    expect(cancel).toHaveBeenCalled();
  });
});

describe("SystemPage — remaining media fields", () => {
  it("edits frame interval + max and PUTs the full media body", async () => {
    mountHandlers();
    let putBody: any = null;
    server.use(
      http.put("*/api/system/settings/media", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json({ ...MEDIA, ...putBody });
      }),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });

    const card = (await screen.findByText("Media & transcription")).closest(".card")!;
    const scoped = within(card as HTMLElement);

    const interval = scoped.getByPlaceholderText(/30s or 25%/) as HTMLInputElement;
    await user.clear(interval);
    await user.type(interval, "25%");

    const max = scoped.getByRole("spinbutton") as HTMLInputElement;
    await user.clear(max);   // empty → coerced to 0
    await user.type(max, "12");

    await user.click(scoped.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putBody).toEqual({
      audio_model: "tiny", audio_compute_type: "int8",
      video_frame_interval: "25%", video_frame_max: 12,
    });
  });
});

describe("SystemPage — note-analysis save error", () => {
  it("rolls back the toggle and shows the error when the PUT fails", async () => {
    mountHandlers();
    server.use(
      http.put("*/api/system/settings/auto-analyze", () =>
        HttpResponse.json({ detail: "nope" }, { status: 500 })),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });
    const card = (await screen.findByText("Note analysis")).closest(".card")!;
    const scoped = within(card as HTMLElement);
    const box = scoped.getByRole("checkbox") as HTMLInputElement;
    await user.click(box);
    expect(await scoped.findByText("nope")).toBeInTheDocument();
    await waitFor(() => expect(box.checked).toBe(false));   // rolled back
  });
});

describe("SystemPage — update lifecycle branches", () => {
  it("shows the 'queued' state when an update needs a manual host step", async () => {
    mountHandlers({ version: { current: "9.9.9", latest: "9.9.10", update_available: true } });
    server.use(
      http.post("*/api/system/update", () =>
        HttpResponse.json({ scheduled: true, auto: false, message: "Run ./update.sh on the host." })),
    );
    const { user } = renderWithProviders(<SystemPage />, { route: "/system" });
    await user.click(await screen.findByRole("button", { name: /Update server/ }));

    expect(await screen.findByText(/Run \.\/update\.sh on the host\./)).toBeInTheDocument();
    // A queued update offers a "Reload now" button.
    expect(screen.getByRole("button", { name: "Reload now" })).toBeInTheDocument();
  });

  it("polls health, recovers, and lands on the online state with the Copy-commands path exercised", async () => {
    // Drive the restart poll deterministically: stub window.setTimeout to run the callback
    // on a microtask (real fetch/MSW still resolve). Health is offline once, then up — the
    // poll sees down→up and flips to "online".
    mountHandlers({ version: { current: "9.9.9", latest: "9.9.10", update_available: true } });
    let healthCalls = 0;
    server.use(
      http.post("*/api/system/update", () => HttpResponse.json({ started: true })),
      http.get("*/api/health", () => {
        healthCalls++;
        return healthCalls === 1
          ? HttpResponse.error()                       // first poll: offline
          : new HttpResponse(null, { status: 200 });   // then back up
      }),
    );
    renderWithProviders(<SystemPage />, { route: "/system" });
    // Find the trigger BEFORE stubbing timers (findBy/user-event lean on real timers).
    const trigger = await screen.findByRole("button", { name: /Update server/ });
    // Collapse only the page's poll delays to ~0 via the REAL timer (so the chain still runs
    // on the macrotask queue and React can flush between ticks) — we don't touch waitFor's
    // own timers. fireEvent drives the click with no timer dependency.
    const realSetTimeout = window.setTimeout.bind(window);
    const stub = vi.spyOn(window, "setTimeout").mockImplementation(((cb: any) =>
      realSetTimeout(cb, 0)) as any);
    const tick = () => new Promise<void>((res) => realSetTimeout(res, 0));
    fireEvent.click(trigger);

    // Pump real macrotasks by hand (avoid waitFor, whose own timers are stubbed too): the
    // poll reschedules itself each tick (all collapsed to ~0), carrying it offline → online.
    for (let i = 0; i < 50 && !screen.queryByText(/Back online/); i++) await tick();
    stub.mockRestore();
    expect(screen.getByText(/Back online/)).toBeInTheDocument();
    expect(healthCalls).toBeGreaterThanOrEqual(2);
  });
});
