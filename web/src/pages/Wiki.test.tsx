// Component-integration (Tier 2) tests for Wiki — the notes/folders browser. Titles are
// "/"-separated, so the page builds a hierarchical tree: pure folders get a collapse
// toggle, articles (a node with its own note) render as <Link to="/note/:slug">.
//
// MSW is the only network mock (setup.ts runs onUnhandledRequest: "error"), so every
// endpoint the page touches on mount is stubbed: the notes list (GET /api/notes — the
// kind defaults to "kb", so a bare mount requests ?kind=kb), the fire-and-forget
// "wiki_viewed" event (POST /api/events/wiki_viewed), and the embedded LinkAuditPanel's
// on-open scan (GET /api/notes/links/audit). The page navigates only via <Link>, so we
// assert link hrefs rather than spying on useNavigate. ../App is stubbed for parity with
// the project's page tests (so nothing drags in the App auth/router provider).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

import Wiki from "./Wiki";

interface NoteRow {
  id: number; title: string; slug: string; kind: string; updated_at: string;
  kb_ingest?: number; tool_access?: number;
}

function row(o: Partial<NoteRow> = {}): NoteRow {
  return {
    id: 1, title: "kb/People/Jeff", slug: "kb-people-jeff", kind: "kb",
    updated_at: "2024-01-01T00:00:00Z", kb_ingest: 1, tool_access: 1, ...o,
  };
}

// Register the three GET/POSTs every Wiki mount performs. `notesFor` lets a test return
// a different set per `kind` so the tab-switch flow can assert the refetch's payload;
// the default ignores kind and returns the same list. Captures the URLs the page hit.
function mountHandlers(notesFor: (kind: string | null) => NoteRow[] = () => [row()]) {
  const calls: string[] = [];
  server.use(
    http.get("*/api/notes", ({ request }) => {
      const url = new URL(request.url);
      calls.push(url.search);
      return HttpResponse.json(notesFor(url.searchParams.get("kind")));
    }),
    http.post("*/api/events/:name", () => HttpResponse.json({ fired: true })),
    http.get("*/api/notes/links/audit", () => HttpResponse.json({ findings: [] })),
  );
  return { calls };
}

beforeEach(() => {
  // jsdom has no matchMedia; nothing in Wiki needs it, but stub for safety/parity.
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }));
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("Wiki", () => {
  it("loads the notes list and renders folders + a note link to /note/:slug", async () => {
    const { calls } = mountHandlers(() => [
      row({ id: 1, title: "kb/People/Jeff Hopkins", slug: "kb-people-jeff" }),
      row({ id: 2, title: "kb/Projects/Q3 Planning", slug: "kb-work-q3", updated_at: "2024-02-01T00:00:00Z" }),
    ]);
    renderWithProviders(<Wiki />, { route: "/wiki" });

    // The recent note (Q3 Planning) branch is left open: its folder + leaf show without
    // any clicking. "kb" and the sub-folder are folder labels in the tree.
    expect(await screen.findByText("Q3 Planning")).toBeInTheDocument();
    expect(screen.getByText("Projects")).toBeInTheDocument();
    expect(screen.getAllByText("kb").length).toBeGreaterThan(0);

    // The leaf article is an anchor pointing at its note route (its accessible name
    // also carries the "KB" kind badge text, so match by substring).
    const link = screen.getByRole("link", { name: /Q3 Planning/ });
    expect(link).toHaveAttribute("href", "/note/kb-work-q3");

    // The default kind is "kb", so the mount request carried ?kind=kb.
    await waitFor(() => expect(calls.some((s) => s.includes("kind=kb"))).toBe(true));
  });

  it("expanding a collapsed folder reveals the note inside it", async () => {
    // Two separate branches; only the recent one (People/Jeff) starts open, so Work is
    // collapsed and its leaf is hidden until its toggle is clicked.
    mountHandlers(() => [
      row({ id: 1, title: "kb/People/Jeff", slug: "kb-people-jeff", updated_at: "2024-03-01T00:00:00Z" }),
      row({ id: 2, title: "kb/Projects/Planning", slug: "kb-work-planning", updated_at: "2024-01-01T00:00:00Z" }),
    ]);
    const { user } = renderWithProviders(<Wiki />, { route: "/wiki" });

    await screen.findByText("Jeff");
    // The Projects branch's leaf is collapsed away initially.
    expect(screen.queryByRole("link", { name: /Planning/ })).not.toBeInTheDocument();

    // The "Projects" folder label sits in a tree-row with a toggle button; expand it.
    const workRow = screen.getByText("Projects").closest(".tree-row") as HTMLElement;
    await user.click(within(workRow).getByRole("button", { name: "toggle" }));

    expect(await screen.findByRole("link", { name: /Planning/ })).toHaveAttribute(
      "href", "/note/kb-work-planning",
    );
  });

  it("the Knowledge base tab is the default and a kb article carries its KB badge", async () => {
    mountHandlers((kind) =>
      kind === "kb" ? [row({ id: 1, title: "kb/Topics/Climbing", slug: "kb-climbing", kind: "kb" })] : [],
    );
    renderWithProviders(<Wiki />, { route: "/wiki" });

    // "Knowledge base" is the selected (primary) tab by default.
    const kbTab = await screen.findByRole("button", { name: "Knowledge base" });
    expect(kbTab).toHaveClass("primary");

    // The kb-kind leaf shows the "KB" kind badge alongside its link.
    const link = await screen.findByRole("link", { name: /Climbing/ });
    expect(within(link).getByText("KB")).toBeInTheDocument();
  });

  it("switching tabs refetches the list with the new kind filter", async () => {
    const { calls } = mountHandlers((kind) =>
      kind === "place"
        ? [row({ id: 9, title: "places/Office", slug: "places-office", kind: "place" })]
        : [row({ id: 1, title: "kb/People/Jeff", slug: "kb-people-jeff", kind: "kb" })],
    );
    const { user } = renderWithProviders(<Wiki />, { route: "/wiki" });

    await screen.findByText("Jeff");
    await user.click(screen.getByRole("button", { name: "Places" }));

    // The refetch sent ?kind=place and the new list rendered.
    await waitFor(() => expect(calls.some((s) => s.includes("kind=place"))).toBe(true));
    expect(await screen.findByRole("link", { name: /Office/ })).toHaveAttribute("href", "/note/places-office");
  });

  it("the 'All' tab refetches without a kind filter", async () => {
    const { calls } = mountHandlers(() => [row()]);
    const { user } = renderWithProviders(<Wiki />, { route: "/wiki" });

    await screen.findByText("Jeff");
    // The mount request carried ?kind=kb (the default tab).
    await waitFor(() => expect(calls.some((s) => s.includes("kind=kb"))).toBe(true));

    await user.click(screen.getByRole("button", { name: "All" }));

    // "All" maps to an empty kind and (here) no q, so the refetch URL has no query
    // string at all — a bare GET /api/notes.
    await waitFor(() => expect(calls.some((s) => s === "")).toBe(true));
  });

  it("typing in the filter input refetches with a q parameter", async () => {
    const { calls } = mountHandlers(() => [row()]);
    const { user } = renderWithProviders(<Wiki />, { route: "/wiki" });

    await screen.findByText("Jeff");
    await user.type(screen.getByPlaceholderText(/Filter notes by title/), "plan");

    await waitFor(() => expect(calls.some((s) => s.includes("q=plan"))).toBe(true));
  });

  it("seeds the filter from the URL query (?q=&kind=) on first load", async () => {
    const { calls } = mountHandlers(() => [row()]);
    renderWithProviders(<Wiki />, { route: "/wiki?q=jeff&kind=place" });

    // The first list request reflects the URL-seeded q and kind.
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    expect(calls[0]).toContain("q=jeff");
    expect(calls[0]).toContain("kind=place");
    // And the "Places" tab is the active one.
    expect(screen.getByRole("button", { name: "Places" })).toHaveClass("primary");
  });

  it("shows the empty state when the list comes back empty", async () => {
    mountHandlers(() => []);
    renderWithProviders(<Wiki />, { route: "/wiki" });

    expect(await screen.findByText("Nothing here yet.")).toBeInTheDocument();
  });

  it("swallows a list fetch error and shows the empty state", async () => {
    server.use(
      http.get("*/api/notes", () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
      http.post("*/api/events/:name", () => HttpResponse.json({ fired: true })),
      http.get("*/api/notes/links/audit", () => HttpResponse.json({ findings: [] })),
    );
    renderWithProviders(<Wiki />, { route: "/wiki" });

    // The mount effect's .catch(() => {}) leaves notes empty, so the page renders its
    // empty-state copy rather than crashing.
    expect(await screen.findByText("Nothing here yet.")).toBeInTheDocument();
  });
});
