// Component-integration (Tier 2) tests for UpdateConsole — the live update-log
// console opened from the System card. It self-polls GET /api/system/update-log on a
// 1.2s window.setTimeout loop and streams the captured update.sh / updater output into
// a scrolling <pre> with a status dot + line and a Copy / (on success) Reload button.
//
// Network: MSW only (setup.ts runs onUnhandledRequest:"error"), so EVERY endpoint each
// flow hits is stubbed via server.use(...). The only endpoints the component touches are:
//   GET /api/system/update-log                         (the primary poll)
//   GET /deploy-status/update.log + /deploy-status/status.json  (Caddy fallback, only
//        reached when the API poll throws AND an access key is stored)
//
// Driving the poll WITHOUT fake timers (those deadlock MSW): we spy on window.setTimeout
// and run the component's 1.2s re-poll on a real 0ms timer, so successive polls fire fast
// while the event loop still turns (fetch/MSW resolve normally). The loop self-cancels on
// unmount (alive=false). Tests that need a state transition flip the MSW handler between
// polls and let waitFor settle on the converged DOM. scrollIntoView is stubbed (jsdom
// has no layout); the component auto-scrolls via scrollTop/scrollHeight, harmless in jsdom.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import { setAccessKey } from "../api";
import UpdateConsole from "./UpdateConsole";

// Run the component's poll re-schedule on a real 0ms timer instead of waiting 1.2s, so
// successive polls happen quickly. We keep the ORIGINAL setTimeout for everything else
// (user-event, clipboard reset, etc.) by capturing it before the spy is installed.
const realSetTimeout = globalThis.setTimeout;
let pollSpy: ReturnType<typeof vi.spyOn> | undefined;
function fastPoll() {
  pollSpy = vi.spyOn(window, "setTimeout").mockImplementation(((fn: any, ms?: number) => {
    // The poll loop schedules at exactly 1200ms — collapse just that to ~0ms.
    return realSetTimeout(fn, ms === 1200 ? 0 : ms) as unknown as number;
  }) as any);
}

beforeEach(() => {
  (Element.prototype as any).scrollIntoView = vi.fn();
});
afterEach(() => {
  pollSpy?.mockRestore();
  pollSpy = undefined;
  vi.restoreAllMocks();
});

describe("UpdateConsole", () => {
  it("mounts, polls /api/system/update-log, and renders the streamed log lines", async () => {
    let hits = 0;
    server.use(
      http.get("*/api/system/update-log", () => {
        hits++;
        return HttpResponse.json({
          log: "==> pulling latest\n==> building image",
          status: { state: "running", phase: "building", at: new Date().toISOString() },
          mtime: Math.floor(Date.now() / 1000),
        });
      }),
    );
    fastPoll();
    renderWithProviders(<UpdateConsole onClose={vi.fn()} />);

    // Title is present immediately; the log body appears after the first poll resolves.
    expect(screen.getByText("Update console")).toBeInTheDocument();
    expect(await screen.findByText(/==> building image/)).toBeInTheDocument();
    await waitFor(() => expect(hits).toBeGreaterThan(0));
    // While running it shows the "Updating … building" status (no Reload yet).
    expect(await screen.findByText(/Updating.*building/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reload" })).not.toBeInTheDocument();
  });

  it("reflects an in-progress -> complete transition and reveals the Reload affordance", async () => {
    let phase = 0;   // 0 = running, then 1+ = ok (a fresh "at" so it isn't treated as stale)
    server.use(
      http.get("*/api/system/update-log", () => {
        const running = phase++ === 0;
        return HttpResponse.json({
          log: running ? "==> building image" : "==> building image\n==> update complete",
          status: running
            ? { state: "running", phase: "building", at: new Date().toISOString() }
            : { state: "ok", phase: "", at: new Date().toISOString() },
          mtime: Math.floor(Date.now() / 1000),
        });
      }),
    );
    fastPoll();
    renderWithProviders(<UpdateConsole onClose={vi.fn()} />);

    // First poll: running. Then a later poll flips to ok → "Update complete" + Reload.
    expect(await screen.findByText(/Updating/)).toBeInTheDocument();
    expect(await screen.findByText("Update complete")).toBeInTheDocument();
    expect(await screen.findByText(/==> update complete/)).toBeInTheDocument();
    const reload = await screen.findByRole("button", { name: "Reload" });
    expect(reload).toBeInTheDocument();
  });

  it("Reload reloads the page and Close calls onClose", async () => {
    server.use(
      http.get("*/api/system/update-log", () =>
        HttpResponse.json({
          log: "done",
          status: { state: "ok", phase: "", at: new Date().toISOString() },
          mtime: Math.floor(Date.now() / 1000),
        }),
      ),
    );
    // jsdom's location.reload is non-configurable; replace the whole object via defineProperty.
    const reload = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, reload },
    });
    fastPoll();
    const onClose = vi.fn();
    const { user } = renderWithProviders(<UpdateConsole onClose={onClose} />);

    await user.click(await screen.findByRole("button", { name: "Reload" }));
    expect(reload).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("shows 'No deploy recorded yet' when the log is empty (placeholder body)", async () => {
    let hits = 0;
    server.use(
      http.get("*/api/system/update-log", () => {
        hits++;
        return HttpResponse.json({ log: "", status: null, mtime: null });
      }),
    );
    fastPoll();
    renderWithProviders(<UpdateConsole onClose={vi.fn()} />);

    await waitFor(() => expect(hits).toBeGreaterThan(0));
    expect(await screen.findByText(/No deploy recorded yet/)).toBeInTheDocument();
    // Empty log → placeholder text shown and Copy is disabled.
    expect(screen.getByText(/Waiting for update output/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy" })).toBeDisabled();
  });

  it("falls back to 'Server offline' when the API poll fails and no Caddy key is set", async () => {
    // No access key stored → fetchViaCaddy() returns null → setReachable(false).
    let caddyHit = false;
    server.use(
      http.get("*/api/system/update-log", () => HttpResponse.error()),
      // Defensive: if a key WERE present these would be hit; assert they are NOT.
      http.get("*/deploy-status/update.log", () => { caddyHit = true; return HttpResponse.text("x"); }),
    );
    fastPoll();
    renderWithProviders(<UpdateConsole onClose={vi.fn()} />);

    expect(await screen.findByText(/Server offline — restarting/)).toBeInTheDocument();
    expect(caddyHit).toBe(false);
  });

  it("uses the Caddy static fallback when the API is down but an access key is stored", async () => {
    setAccessKey("secret-key");   // enables the gated /deploy-status/* fallback
    let logHit = false;
    let statusHit = false;
    server.use(
      http.get("*/api/system/update-log", () => HttpResponse.error()),
      http.get("*/deploy-status/update.log", () => {
        logHit = true;
        return HttpResponse.text("==> caddy-served tail");
      }),
      http.get("*/deploy-status/status.json", () => {
        statusHit = true;
        return HttpResponse.json({ state: "failed", phase: "restarting", at: new Date().toISOString() });
      }),
    );
    fastPoll();
    renderWithProviders(<UpdateConsole onClose={vi.fn()} />);

    expect(await screen.findByText(/==> caddy-served tail/)).toBeInTheDocument();
    expect(await screen.findByText("Update failed")).toBeInTheDocument();
    await waitFor(() => { expect(logHit).toBe(true); expect(statusHit).toBe(true); });
  });
});
