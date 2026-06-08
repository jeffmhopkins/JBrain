// Component-integration (Tier 2) tests for NoteHistoryPage — the full revision-history
// view for a single note. It fetches the version timeline + the note title on mount and
// reuses the shared HistoryTimeline / VersionViewer / DiffView flow. Rendered inside a
// real /note/:slug/history route so useParams() resolves the slug.
//
// Network: every endpoint the page touches is mocked via MSW (onUnhandledRequest:"error").
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router-dom";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

import NoteHistoryPage from "./NoteHistoryPage";

const TIMELINE = [
  { version_id: 9, is_current: true, title: "notes/Groceries", source: "user",
    conversation_id: null, note: null, created_at: "2024-01-02T00:00:00Z", size: 20 },
  { version_id: 8, is_current: false, title: "notes/Groceries", source: "ai",
    conversation_id: null, note: null, created_at: "2024-01-01T00:00:00Z", size: 18 },
];

// jsdom needs matchMedia for some shared components; provide a calm phone-layout stub.
beforeEach(() => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }));
});
afterEach(() => { vi.unstubAllGlobals(); cleanup(); });

function renderAt(slug = "groceries") {
  return renderWithProviders(
    <Routes><Route path="/note/:slug/history" element={<NoteHistoryPage />} /></Routes>,
    { route: `/note/${slug}/history` },
  );
}

describe("NoteHistoryPage", () => {
  it("loads the timeline + title and renders the version list with a count", async () => {
    server.use(
      http.get("*/api/notes/:slug/versions", () => HttpResponse.json(TIMELINE)),
      http.get("*/api/notes/:slug", () => HttpResponse.json({ title: "notes/Groceries" })),
    );
    renderAt();

    // Heading + the leaf-title link + the "2 versions" suffix.
    expect(await screen.findByRole("heading", { name: "Version history" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Groceries" })).toBeInTheDocument();
    expect(await screen.findByText(/2 versions/)).toBeInTheDocument();
    // The timeline renders both entries (Current + a Compare for the older one).
    expect(await screen.findByText("Current")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Compare with current/ })).toBeInTheDocument();
  });

  it("opens a diff for a past version", async () => {
    let diffRequested = false;
    server.use(
      http.get("*/api/notes/:slug/versions", () => HttpResponse.json(TIMELINE)),
      http.get("*/api/notes/:slug", () => HttpResponse.json({ title: "notes/Groceries" })),
      http.get("*/api/notes/:slug/diff/:from/:to", () => {
        diffRequested = true;
        return HttpResponse.json({ before: "old", after: "new", title_changed: false });
      }),
    );
    const { user } = renderAt();

    await user.click(await screen.findByRole("button", { name: /Compare with current/ }));

    await waitFor(() => expect(diffRequested).toBe(true));
    expect(await screen.findByText("Changes → current")).toBeInTheDocument();
  });

  it("views a past version and restores it", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let restoreBody: any = null;
    server.use(
      http.get("*/api/notes/:slug/versions", () => HttpResponse.json(TIMELINE)),
      http.get("*/api/notes/:slug", () => HttpResponse.json({ title: "notes/Groceries" })),
      http.get("*/api/notes/:slug/versions/:vid", () =>
        HttpResponse.json({ content_md: "An older revision body." })),
      http.post("*/api/notes/:slug/restore", async ({ request }) => {
        restoreBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderAt();

    await screen.findByText("Current");
    // The older entry's view button is labelled with its relative time and sits just
    // above its Compare button.
    const compare = await screen.findByText(/Compare with current/);
    const olderView = compare.closest(".list-item")!.querySelector("button")! as HTMLButtonElement;
    await user.click(olderView);

    expect(await screen.findByText("An older revision body.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Restore this version/ }));
    await waitFor(() => expect(restoreBody).toEqual({ version_id: 8 }));
  });

  it("renders the empty-history state when there are no versions", async () => {
    server.use(
      http.get("*/api/notes/:slug/versions", () => HttpResponse.json([])),
      http.get("*/api/notes/:slug", () => HttpResponse.json({ title: "notes/Groceries" })),
    );
    renderAt();

    expect(await screen.findByText("No history yet.")).toBeInTheDocument();
    // No version count when the timeline is empty.
    expect(screen.queryByText(/version/)).not.toBeInTheDocument();
  });

  it("survives a versions load error — shows empty history, not a crash", async () => {
    server.use(
      http.get("*/api/notes/:slug/versions", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 })),
      http.get("*/api/notes/:slug", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    renderAt("missing");

    // The catch() swallows the error and loading clears, so the empty-history copy shows.
    expect(await screen.findByText("No history yet.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Version history" })).toBeInTheDocument();
  });
});
