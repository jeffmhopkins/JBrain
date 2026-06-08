// Component-integration (Tier 2) tests for VersionViewer (exported from VersionViewer.tsx)
// — the modal that views a single note version and offers a restore. It fetches the
// version body (GET /api/notes/:slug/versions/:id) on mount and renders it as markdown
// inside the shared Modal; for a non-current version it shows a "Restore this version"
// footer that confirms, POSTs /api/notes/:slug/restore, and calls onRestored.
//
// Network goes through MSW (setup.ts uses onUnhandledRequest:"error", so each flow stubs
// the routes it touches via server.use). It calls useNavigate() (via the markdown link
// renderer) so we render under the router. restore() calls window.confirm — we stub it.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";

import { VersionViewer, type TimelineEntry } from "./VersionViewer";

const VERSION_PATH = "*/api/notes/:slug/versions/:vid";
const RESTORE_PATH = "*/api/notes/:slug/restore";

function entry(o: Partial<TimelineEntry> = {}): TimelineEntry {
  return {
    version_id: 42,
    is_current: false,
    title: "Sourdough",
    source: "user",
    conversation_id: null,
    note: null,
    created_at: "2026-05-01 12:00:00",
    size: 100,
    ...o,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("VersionViewer — load + display", () => {
  it("fetches the version body on mount and renders it as markdown in the modal", async () => {
    let asked: { slug: string | null; vid: string | null } = { slug: null, vid: null };
    server.use(
      http.get(VERSION_PATH, ({ params }) => {
        asked = { slug: String(params.slug), vid: String(params.vid) };
        return HttpResponse.json({ content_md: "# Loaf\n\nFeed the starter." });
      }),
    );
    renderWithProviders(
      <VersionViewer slug="sourdough" version={entry()} onClose={() => {}} onRestored={() => {}} />,
    );
    // The fetched body renders.
    expect(await screen.findByText(/Feed the starter\./)).toBeInTheDocument();
    // It requested the right note + version.
    expect(asked).toEqual({ slug: "sourdough", vid: "42" });
    // Header title is "Version from <date>" for a non-current version, with a source badge.
    expect(screen.getByText(/Version from/)).toBeInTheDocument();
    expect(screen.getByText("user")).toBeInTheDocument();
  });

  it("titles the modal 'Current version' and hides the restore footer for the current version", async () => {
    server.use(http.get(VERSION_PATH, () => HttpResponse.json({ content_md: "body" })));
    renderWithProviders(
      <VersionViewer slug="s" version={entry({ is_current: true })} onClose={() => {}} onRestored={() => {}} />,
    );
    expect(await screen.findByText("body")).toBeInTheDocument();
    expect(screen.getByText("Current version")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Restore this version/ })).not.toBeInTheDocument();
  });

  it("shows '(failed to load)' if the version fetch errors", async () => {
    server.use(http.get(VERSION_PATH, () => HttpResponse.json({ detail: "boom" }, { status: 500 })));
    renderWithProviders(
      <VersionViewer slug="s" version={entry()} onClose={() => {}} onRestored={() => {}} />,
    );
    expect(await screen.findByText(/\(failed to load\)/)).toBeInTheDocument();
  });
});

describe("VersionViewer — restore + close", () => {
  it("restores after confirm: POSTs the version_id and calls onRestored", async () => {
    let posted: any = null;
    server.use(
      http.get(VERSION_PATH, () => HttpResponse.json({ content_md: "old body" })),
      http.post(RESTORE_PATH, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const onRestored = vi.fn();
    const { user } = renderWithProviders(
      <VersionViewer slug="sourdough" version={entry()} onClose={() => {}} onRestored={onRestored} />,
    );
    await screen.findByText("old body");
    await user.click(screen.getByRole("button", { name: /Restore this version/ }));
    await waitFor(() => expect(onRestored).toHaveBeenCalledTimes(1));
    expect(posted).toEqual({ version_id: 42 });
  });

  it("does nothing when the restore confirm is declined", async () => {
    let restoreHit = false;
    server.use(
      http.get(VERSION_PATH, () => HttpResponse.json({ content_md: "old body" })),
      http.post(RESTORE_PATH, () => { restoreHit = true; return HttpResponse.json({ ok: true }); }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const onRestored = vi.fn();
    const { user } = renderWithProviders(
      <VersionViewer slug="s" version={entry()} onClose={() => {}} onRestored={onRestored} />,
    );
    await screen.findByText("old body");
    await user.click(screen.getByRole("button", { name: /Restore this version/ }));
    expect(restoreHit).toBe(false);
    expect(onRestored).not.toHaveBeenCalled();
  });

  it("closes via the modal Close button (onClose)", async () => {
    server.use(http.get(VERSION_PATH, () => HttpResponse.json({ content_md: "x" })));
    const onClose = vi.fn();
    const { user } = renderWithProviders(
      <VersionViewer slug="s" version={entry()} onClose={onClose} onRestored={() => {}} />,
    );
    await screen.findByText("x");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
