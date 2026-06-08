// Component-integration (Tier 2) tests for NotePage — the note read/edit/history surface.
// MSW is the only network mock (setup.ts runs onUnhandledRequest: "error"), so every
// endpoint the page touches on mount + per flow is stubbed. The page reads useAuth()
// from ../App (only `appTz`), the health store via useCapability, and useIsDesktop's
// matchMedia — all stubbed test-side without touching production code.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router-dom";
import { renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import { __reset, ingestVerify } from "../health";
import { __resetToasts } from "../toast";

// NotePage imports `useAuth` from ../App for the display timezone. Stub the module so we
// don't drag in the whole auth/router App tree (and its provider) just for `appTz`.
vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

import NotePage from "./NotePage";

const READY_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: false } },
  embeddings: { state: "ready" }, transcription: { state: "ready" },
  push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
};

// A complete Note payload as the server returns it. Overridable per test.
function note(overrides: Record<string, unknown> = {}) {
  return {
    id: 1, title: "notes/Groceries", slug: "groceries",
    content_md: "Milk and **eggs**.", kind: "note",
    created_at: "2024-01-01T00:00:00Z", updated_at: "2024-01-01T00:00:00Z",
    lat: null, lon: null, location_label: null,
    backlinks: [], tags: ["food"],
    kb_ingest: 1, tool_access: 1,
    redirect_to: null, redirect_to_slug: null,
    ...overrides,
  };
}

const TIMELINE = [
  { version_id: 9, is_current: true, title: "notes/Groceries", source: "user",
    conversation_id: null, note: null, created_at: "2024-01-02T00:00:00Z", size: 20 },
  { version_id: 8, is_current: false, title: "notes/Groceries", source: "ai",
    conversation_id: null, note: null, created_at: "2024-01-01T00:00:00Z", size: 18 },
];

// Register the GETs every render of a `kind: "note"` page performs on mount:
// the note, its version timeline, the rail's Attachments list, and the AI analysis
// sidecar. Returns a place to read the latest note state from for assertions.
function mountHandlers(noteData = note()) {
  const state = { current: noteData };
  server.use(
    http.get("*/api/notes/:slug/versions", () => HttpResponse.json(TIMELINE)),
    http.get("*/api/notes/:slug/attachments", () => HttpResponse.json([])),
    http.get("*/api/notes/:slug/analysis", () => HttpResponse.json({})),
    http.get("*/api/notes/:slug", () => HttpResponse.json(state.current)),
  );
  return state;
}

beforeEach(() => {
  __reset();
  __resetToasts();
  ingestVerify({ capabilities: READY_CAPS });   // seed health so useCapability("llm") is ready
  // jsdom has no matchMedia; useIsDesktop() needs it. false => phone layout (rail still renders).
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }));
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("NotePage", () => {
  it("renders a fetched note's title and markdown", async () => {
    mountHandlers();
    renderWithProviders(<NotePage />, { route: "/note/groceries" });

    expect(await screen.findByRole("heading", { name: "Groceries" })).toBeInTheDocument();
    // Markdown body is rendered (the bold span comes through react-markdown).
    expect(await screen.findByText("eggs")).toBeInTheDocument();
    // A tag chip from the payload.
    expect(screen.getByText("#food")).toBeInTheDocument();
  });

  it("surfaces a load error instead of the note", async () => {
    server.use(
      http.get("*/api/notes/:slug/versions", () => HttpResponse.json([])),
      http.get("*/api/notes/:slug", () =>
        HttpResponse.json({ detail: "Note not found" }, { status: 404 })),
    );
    renderWithProviders(<NotePage />, { route: "/note/missing" });

    expect(await screen.findByText("Note not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Back to wiki/ })).toBeInTheDocument();
  });

  it("edit → save issues a PUT with the new body and reflects the saved content", async () => {
    const state = mountHandlers();
    let putBody: any = null;
    server.use(
      http.put("*/api/notes/:slug", async ({ request }) => {
        putBody = await request.json();
        // Persist so the post-save reload() returns the edited content.
        state.current = note({ content_md: putBody.content_md, title: putBody.title });
        return HttpResponse.json({ slug: "groceries" });
      }),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Groceries" });

    // Open the kebab actions menu and click Edit.
    await user.click(screen.getByRole("button", { name: "Page actions" }));
    await user.click(await screen.findByRole("menuitem", { name: /Edit/ }));

    // The edit textarea is seeded with the current markdown; rewrite it.
    const textarea = await screen.findByRole("textbox", { name: "" }).catch(() => null) as HTMLTextAreaElement | null;
    const area = textarea ?? (document.querySelector(".note-edit-area") as HTMLTextAreaElement);
    await user.clear(area);
    await user.type(area, "Bread only.");

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putBody).toEqual({ title: "notes/Groceries", content_md: "Bread only." });
    // After save the editor closes and the reloaded content shows.
    expect(await screen.findByText("Bread only.")).toBeInTheDocument();
  });

  it("surfaces a save error and keeps the editor open", async () => {
    mountHandlers();
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    server.use(
      http.put("*/api/notes/:slug", () =>
        HttpResponse.json({ detail: "Save failed on server" }, { status: 500 })),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Groceries" });
    await user.click(screen.getByRole("button", { name: "Page actions" }));
    await user.click(await screen.findByRole("menuitem", { name: /Edit/ }));

    const area = document.querySelector(".note-edit-area") as HTMLTextAreaElement;
    await user.clear(area);
    await user.type(area, "x");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("Save failed on server"));
    // Editor stays open (Save button still present) so the edit isn't lost.
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("loads version history and opens a diff for a past version", async () => {
    mountHandlers();
    let diffRequested = false;
    server.use(
      http.get("*/api/notes/:slug/diff/:from/:to", () => {
        diffRequested = true;
        return HttpResponse.json({ before: "old", after: "new", title_changed: false });
      }),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    // The rail's History timeline renders both entries from /versions.
    expect(await screen.findByText("Current")).toBeInTheDocument();
    const compare = await screen.findByRole("button", { name: /Compare with current/ });
    await user.click(compare);

    // DiffView modal fetches the diff and shows the heading.
    await waitFor(() => expect(diffRequested).toBe(true));
    expect(await screen.findByText("Changes → current")).toBeInTheDocument();
  });

  it("views a past version and restores it", async () => {
    const state = mountHandlers();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let restoreBody: any = null;
    server.use(
      http.get("*/api/notes/:slug/versions/:vid", () =>
        HttpResponse.json({ content_md: "An older revision body." })),
      http.post("*/api/notes/:slug/restore", async ({ request }) => {
        restoreBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    void state;
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    // The non-current timeline entry's button is labelled with its relative time;
    // click it to open the VersionViewer.
    await screen.findByText("Current");
    const entries = await screen.findAllByText(/Compare with current/);
    // Open the viewer for the older version (its "view" button sits just above Compare).
    const olderView = entries[0].closest(".list-item")!
      .querySelector("button")! as HTMLButtonElement;
    await user.click(olderView);

    // Viewer fetches and renders the old content, then Restore posts version_id.
    expect(await screen.findByText("An older revision body.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Restore this version/ }));
    await waitFor(() => expect(restoreBody).toEqual({ version_id: 8 }));
  });

  it("renames a note (slug change) and navigates to the new slug", async () => {
    const state = mountHandlers();
    server.use(
      http.put("*/api/notes/:slug", async ({ request }) => {
        const body: any = await request.json();
        state.current = note({ title: body.title, slug: "kb-jeff" });
        return HttpResponse.json({ slug: "kb-jeff" });   // slug changed by the rename
      }),
    );
    // Render inside a real /note/:slug route so useParams() resolves the slug and a
    // navigate() to the renamed slug re-drives the load effect (the bare-element render
    // used elsewhere works only because the wildcard handlers ignore the slug).
    const { user } = renderWithProviders(
      <Routes><Route path="/note/:slug" element={<NotePage />} /></Routes>,
      { route: "/note/groceries" },
    );

    await screen.findByRole("heading", { name: "Groceries" });
    await user.click(screen.getByRole("button", { name: "Page actions" }));
    await user.click(await screen.findByRole("menuitem", { name: /Edit/ }));

    const titleInput = screen.getByPlaceholderText(/Title —/) as HTMLInputElement;
    await user.clear(titleInput);
    await user.type(titleInput, "kb/Jeff");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // After a slug-changing rename the route should navigate to /note/kb-jeff,
    // which refetches and renders the new leaf title.
    expect(await screen.findByRole("heading", { name: "Jeff" })).toBeInTheDocument();
  });

  it("blocks an empty-title save without issuing a PUT", async () => {
    mountHandlers();
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    let putFired = false;
    server.use(http.put("*/api/notes/:slug", () => { putFired = true; return HttpResponse.json({ slug: "groceries" }); }));
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Groceries" });
    await user.click(screen.getByRole("button", { name: "Page actions" }));
    await user.click(await screen.findByRole("menuitem", { name: /Edit/ }));

    const titleInput = screen.getByPlaceholderText(/Title —/) as HTMLInputElement;
    await user.clear(titleInput);
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(alertSpy).toHaveBeenCalledWith("Title can't be empty.");
    expect(putFired).toBe(false);
    void within;   // keep import used if a future assertion needs scoping
  });
});
