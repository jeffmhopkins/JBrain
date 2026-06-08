// Component-integration (Tier 2) tests for NotificationHistoryPage.tsx — the
// read-only archive of notifications dismissed in the last 24h. The page GETs
// /api/reviews/history once on mount and renders each item (title / optional
// message / optional "Open" link). It is read-only (no dismiss). On no items it
// shows an empty state ONLY after the load settles (the `loaded` flag), and an
// error leaves it empty (the catch swallows it). MSW is the only network mock
// (setup.ts runs onUnhandledRequest:"error"); useAuth() is mocked from ../App
// to supply appTz for fmtTsShort.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

vi.mock("../App", () => ({
  useAuth: () => ({ appTz: "UTC" }),
}));

import NotificationHistoryPage from "./NotificationHistoryPage";

const HISTORY = [
  { id: 11, title: "Reviewed the budget", message: "All good", link_slug: "budget", created_at: "2024-01-01T00:00:00Z", dismissed_at: "2024-01-01T01:00:00Z" },
  { id: 12, title: "System pinged you", message: "", link_slug: null, created_at: "2024-01-02T00:00:00Z", dismissed_at: null },
];

beforeEach(() => {
  server.use(http.get("*/api/reviews/history", () => HttpResponse.json(HISTORY)));
});
afterEach(() => cleanup());

describe("NotificationHistoryPage", () => {
  it("loads and renders the dismissed-notification history", async () => {
    renderWithProviders(<NotificationHistoryPage />);

    expect(await screen.findByText("Reviewed the budget")).toBeInTheDocument();
    expect(screen.getByText("System pinged you")).toBeInTheDocument();
    // The non-empty message renders; the empty one contributes no <p>.
    expect(screen.getByText("All good")).toBeInTheDocument();
    // Static header copy is always present.
    expect(screen.getByText("Notification History")).toBeInTheDocument();
  });

  it("shows an Open link only for an item with a link_slug, and is read-only (no Dismiss)", async () => {
    renderWithProviders(<NotificationHistoryPage />);

    await screen.findByText("Reviewed the budget");
    const links = screen.getAllByRole("link", { name: "Open" });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "/note/budget");
    // This is an archive — there is no dismiss affordance.
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
  });

  it("shows the empty state after the load settles when there is no history", async () => {
    server.use(http.get("*/api/reviews/history", () => HttpResponse.json([])));
    renderWithProviders(<NotificationHistoryPage />);

    expect(await screen.findByText("Nothing dismissed in the last 24 hours.")).toBeInTheDocument();
  });

  it("stays empty (no crash) when the history load fails", async () => {
    server.use(http.get("*/api/reviews/history", () => new HttpResponse(null, { status: 500 })));
    renderWithProviders(<NotificationHistoryPage />);

    // The catch still flips `loaded`, so the empty state appears and no items show.
    expect(await screen.findByText("Nothing dismissed in the last 24 hours.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Reviewed the budget")).not.toBeInTheDocument());
  });
});
