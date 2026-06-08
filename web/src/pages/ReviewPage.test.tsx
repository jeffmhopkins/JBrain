// Component-integration (Tier 2) tests for ReviewPage.tsx — the Review inbox.
// The page GETs /api/reviews on mount and renders each item (title / message /
// optional "Open entry" link), POSTs /api/reviews/{id}/dismiss on Dismiss (then
// drops the row), shows an empty state when there's nothing to review, and stays
// empty on a load error (the catch swallows it). MSW is the only network mock
// (setup.ts runs onUnhandledRequest:"error"), so every endpoint the page touches
// is stubbed per test. useAuth() is mocked from ../App to supply appTz for fmtTs.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

vi.mock("../App", () => ({
  useAuth: () => ({ appTz: "UTC" }),
}));

import ReviewPage from "./ReviewPage";

const ITEMS = [
  { id: 7, title: "Review the groceries note", message: "Looks stale", link_slug: "groceries", created_at: "2024-01-01T00:00:00Z" },
  { id: 9, title: "Daily review", message: "", link_slug: null, created_at: "2024-01-02T00:00:00Z" },
];

beforeEach(() => {
  server.use(http.get("*/api/reviews", () => HttpResponse.json(ITEMS)));
});
afterEach(() => cleanup());

describe("ReviewPage", () => {
  it("loads and renders review items with their title, message and link", async () => {
    renderWithProviders(<ReviewPage />);

    // Both items load: titles + the (non-empty) message render.
    expect(await screen.findByText("Review the groceries note")).toBeInTheDocument();
    expect(screen.getByText("Daily review")).toBeInTheDocument();
    expect(screen.getByText("Looks stale")).toBeInTheDocument();

    // The item with a link_slug gets an "Open entry" link to /note/<slug>; the
    // link-less item shows only a Dismiss button.
    const open = screen.getByRole("link", { name: "Open entry" });
    expect(open).toHaveAttribute("href", "/note/groceries");
    expect(screen.getAllByRole("link", { name: "Open entry" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Dismiss" })).toHaveLength(2);
  });

  it("dismiss POSTs /api/reviews/{id}/dismiss and removes the item from the list", async () => {
    let dismissedId = 0;
    server.use(
      http.post("*/api/reviews/:id/dismiss", ({ params }) => {
        dismissedId = Number(params.id);
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<ReviewPage />);

    await screen.findByText("Review the groceries note");
    // Dismiss the first item (the one with the link).
    const firstDismiss = screen.getAllByRole("button", { name: "Dismiss" })[0];
    await user.click(firstDismiss);

    // The request fired for id 7 and the row is removed; the other item stays.
    await waitFor(() => expect(dismissedId).toBe(7));
    await waitFor(() => expect(screen.queryByText("Review the groceries note")).not.toBeInTheDocument());
    expect(screen.getByText("Daily review")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Dismiss" })).toHaveLength(1);
  });

  it("shows the empty state when there are no items to review", async () => {
    server.use(http.get("*/api/reviews", () => HttpResponse.json([])));
    renderWithProviders(<ReviewPage />);

    expect(await screen.findByText(/Nothing to review/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
  });

  it("stays empty (no crash) when the reviews load fails", async () => {
    server.use(http.get("*/api/reviews", () => new HttpResponse(null, { status: 500 })));
    renderWithProviders(<ReviewPage />);

    // The catch swallows the error → list stays empty and the empty state shows.
    expect(await screen.findByText(/Nothing to review/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
  });
});
