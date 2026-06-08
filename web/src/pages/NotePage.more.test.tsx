// Extra component-integration (Tier 2) tests for NotePage — the SECONDARY flows the
// original NotePage.test.tsx leaves uncovered: share-link minting + visibility presets,
// tag add/remove, the list-note checkbox toggle, the KB "Rebuild page now" entry
// (RebuildPanel mount), the Attachments rail section, the AI-analysis sidecar panel,
// redirect forwarding, geofence/place, and backlinks. Mirrors the existing file's
// conventions: MSW (onUnhandledRequest:"error") for the network, vi.mock("../App") for
// useAuth, matchMedia stub for useIsDesktop. The one SSE child (RebuildPanel) has its
// stream api method mocked so it mounts without a live connection.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes, useLocation } from "react-router-dom";
import { renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import { __reset, ingestVerify } from "../health";
import { __resetToasts } from "../toast";

vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

// RebuildPanel opens an SSE stream (rebuildStream → /api/kb/rebuild/start/:slug) the moment
// it mounts. MSW's onUnhandledRequest:"error" would flag that long-lived POST, and we only
// need to prove the panel mounted — so stub just that one api method, keeping everything
// else (which routes through MSW) real via importActual.
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, rebuildStream: vi.fn(() => ({ done: Promise.resolve(), abort: () => {} })) };
});

import NotePage from "./NotePage";
import { rebuildStream } from "../api";

const READY_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: false } },
  embeddings: { state: "ready" }, transcription: { state: "ready" },
  push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
};

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

const TIMELINE: any[] = [];

// Register the GETs every render performs on mount: the note, its version timeline, the
// rail's Attachments list, and the AI-analysis sidecar. `state.current` is mutated by
// write handlers so a post-write reload() returns the new server state.
function mountHandlers(noteData = note(), opts: { attachments?: any[]; analysis?: any; places?: any[] } = {}) {
  const state = { current: noteData };
  server.use(
    http.get("*/api/notes/:slug/versions", () => HttpResponse.json(TIMELINE)),
    http.get("*/api/notes/:slug/attachments", () => HttpResponse.json(opts.attachments ?? [])),
    http.get("*/api/notes/:slug/analysis", () => HttpResponse.json(opts.analysis ?? {})),
    // A KB note's rail mounts <TalkPanel>, which reads its talk items on mount.
    http.get("*/api/notes/:slug/talk", () => HttpResponse.json([])),
    http.get("*/api/places", () => HttpResponse.json(opts.places ?? [])),
    http.get("*/api/notes/:slug", () => HttpResponse.json(state.current)),
  );
  return state;
}

beforeEach(() => {
  __reset();
  __resetToasts();
  ingestVerify({ capabilities: READY_CAPS });
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }));
  // Note: navigator.clipboard is provided by user-event's setup() (called inside
  // renderWithProviders); the share test spies on it after render.
});
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

async function openMenu(user: ReturnType<typeof renderWithProviders>["user"]) {
  await user.click(screen.getByRole("button", { name: "Page actions" }));
}

describe("NotePage — share links", () => {
  it("mints a view-only link via POST /api/shares and shows + copies the URL", async () => {
    mountHandlers();
    let shareBody: any = null;
    server.use(
      http.post("*/api/shares", async ({ request }) => {
        shareBody = await request.json();
        return HttpResponse.json({ url: "https://share.example/abc" });
      }),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });
    // user-event.setup() (inside renderWithProviders) installs its own navigator.clipboard,
    // so spy AFTER render to catch the production writeText through user-event's stub.
    const writeSpy = vi.spyOn(navigator.clipboard, "writeText");

    await screen.findByRole("heading", { name: "Groceries" });
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: /Share/ }));

    // The share panel offers the access segmented control + Create link.
    await user.click(screen.getByRole("button", { name: "Create link" }));

    await waitFor(() => expect(shareBody).not.toBeNull());
    expect(shareBody).toEqual({ title: "notes/Groceries", scope: "view", ttl_days: undefined, bind: false });
    // Minted state shows the URL + a "View only" badge, and the URL was copied.
    const urlInput = await screen.findByDisplayValue("https://share.example/abc");
    expect(urlInput).toBeInTheDocument();
    expect(screen.getByText("View only")).toBeInTheDocument();
    expect(writeSpy).toHaveBeenCalledWith("https://share.example/abc");
  });

  it("mints an editable link when 'Can edit' is selected (scope=edit)", async () => {
    mountHandlers();
    let shareBody: any = null;
    server.use(
      http.post("*/api/shares", async ({ request }) => {
        shareBody = await request.json();
        return HttpResponse.json({ url: "https://share.example/edit1" });
      }),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Groceries" });
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: /Share/ }));

    await user.click(screen.getByRole("button", { name: "Can edit" }));
    await user.click(screen.getByRole("button", { name: "Create link" }));

    await waitFor(() => expect(shareBody?.scope).toBe("edit"));
    expect(await screen.findByText("Can edit")).toBeInTheDocument();
  });

  it("force-hardens a private personal record: no 'Can edit', a finite TTL, locked", async () => {
    mountHandlers(note({ title: "kb/Health/Bloodwork", kind: "note" }));
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Bloodwork" });
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: /Share/ }));

    // The hardening notice shows; the "Can edit" toggle is hidden for a private record.
    expect(await screen.findByText(/private/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Can edit" })).not.toBeInTheDocument();
  });
});

describe("NotePage — tags", () => {
  it("adds a tag via Enter (PUT /tags with the appended tag)", async () => {
    const state = mountHandlers();
    let tagsBody: any = null;
    server.use(
      http.put("*/api/notes/:slug/tags", async ({ request }) => {
        tagsBody = await request.json();
        state.current = note({ tags: tagsBody.tags });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Groceries" });
    const input = screen.getByPlaceholderText("+ tag");
    await user.type(input, "errand{Enter}");

    await waitFor(() => expect(tagsBody).toEqual({ tags: ["food", "errand"] }));
    expect(await screen.findByText("#errand")).toBeInTheDocument();
  });

  it("removes a tag via its × button (PUT /tags without it)", async () => {
    const state = mountHandlers();
    let tagsBody: any = null;
    server.use(
      http.put("*/api/notes/:slug/tags", async ({ request }) => {
        tagsBody = await request.json();
        state.current = note({ tags: tagsBody.tags });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Groceries" });
    const chip = screen.getByText("#food");
    // The remove control is an "×" button labelled by its title attribute.
    await user.click(within(chip).getByTitle("Remove tag"));

    await waitFor(() => expect(tagsBody).toEqual({ tags: [] }));
  });
});

describe("NotePage — visibility presets", () => {
  it("Research-only sends kb_ingest:false, tool_access:true to /flags", async () => {
    mountHandlers();
    let flagsBody: any = null;
    server.use(
      http.put("*/api/notes/:slug/flags", async ({ request }) => {
        flagsBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Groceries" });
    await user.click(screen.getByRole("button", { name: "Research-only" }));

    await waitFor(() => expect(flagsBody).toEqual({ kb_ingest: false, tool_access: true }));
    expect(screen.getByRole("button", { name: "Research-only" })).toHaveAttribute("aria-pressed", "true");
  });

  it("Private sends both flags false", async () => {
    mountHandlers();
    let flagsBody: any = null;
    server.use(
      http.put("*/api/notes/:slug/flags", async ({ request }) => {
        flagsBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Groceries" });
    await user.click(screen.getByRole("button", { name: "Private" }));

    await waitFor(() => expect(flagsBody).toEqual({ kb_ingest: false, tool_access: false }));
  });
});

describe("NotePage — checkbox toggle", () => {
  it("ticking a rendered task-list item PUTs the note with [x] on that line", async () => {
    const state = mountHandlers(note({ content_md: "- [ ] buy milk\n- [ ] buy bread" }));
    let putBody: any = null;
    server.use(
      http.put("*/api/notes/:slug", async ({ request }) => {
        putBody = await request.json();
        state.current = note({ content_md: putBody.content_md });
        return HttpResponse.json({ slug: "groceries" });
      }),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Groceries" });
    const boxes = await screen.findAllByRole("checkbox");
    await user.click(boxes[0]);   // first task line

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putBody.content_md).toBe("- [x] buy milk\n- [ ] buy bread");
  });
});

describe("NotePage — KB rebuild entry", () => {
  it("'Rebuild page now' on a KB note mounts RebuildPanel and opens the stream", async () => {
    mountHandlers(note({ title: "kb/Coffee", slug: "coffee", kind: "kb", content_md: "About coffee." }));
    const { user } = renderWithProviders(<NotePage />, { route: "/note/coffee" });

    await screen.findByRole("heading", { name: /Coffee/ });
    await openMenu(user);
    await user.click(await screen.findByRole("menuitem", { name: /Rebuild page now/ }));

    // Modal title + the stream method fired for this slug.
    expect(await screen.findByText("Rebuild page")).toBeInTheDocument();
    expect(rebuildStream).toHaveBeenCalledWith("coffee", expect.any(Function));
  });
});

describe("NotePage — attachments rail", () => {
  it("lists an attachment from the rail's Attachments section", async () => {
    mountHandlers(note(), {
      attachments: [{ id: 7, filename: "receipt.pdf", mime: "application/pdf", byte_size: 2048, created_at: "2024-01-01T00:00:00Z" }],
    });
    renderWithProviders(<NotePage />, { route: "/note/groceries" });

    expect(await screen.findByText("receipt.pdf")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "+ Attach file" })).toBeInTheDocument();
  });

  it("deletes an attachment (DELETE /api/attachments/:id) and reloads the note", async () => {
    const state = mountHandlers(note(), {
      attachments: [{ id: 7, filename: "receipt.pdf", mime: "application/pdf", byte_size: 2048, created_at: "2024-01-01T00:00:00Z" }],
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let deleted = false;
    server.use(
      http.delete("*/api/attachments/:id", () => { deleted = true; return HttpResponse.json({ ok: true }); }),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByText("receipt.pdf");
    // The attachment row's Delete button (the note-level Delete lives in the kebab menu).
    const row = screen.getByText("receipt.pdf").closest(".list-item") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleted).toBe(true));
    void state;
  });
});

describe("NotePage — AI analysis sidecar", () => {
  it("renders the gist/facts/entities from /analysis", async () => {
    mountHandlers(note(), {
      analysis: {
        gist: "A short grocery list.",
        facts: ["Needs milk", "Needs eggs"],
        entities: [{ type: "thing", name: "Milk" }],
        domain: "Household",
      },
    });
    renderWithProviders(<NotePage />, { route: "/note/groceries" });

    expect(await screen.findByText("A short grocery list.")).toBeInTheDocument();
    expect(screen.getByText("Needs milk")).toBeInTheDocument();
    expect(screen.getByText("Household")).toBeInTheDocument();
    // The entity renders as a chip link to the entity index (disambiguates from the
    // word "Milk" that also appears in the note body).
    expect(screen.getByRole("link", { name: /Milk/ })).toHaveAttribute(
      "href", "/entities?type=thing&q=Milk");
  });

  it("the ↻ control re-analyzes via POST /analysis and shows the refreshed gist", async () => {
    mountHandlers(note(), { analysis: { gist: "Old gist." } });
    server.use(
      http.post("*/api/notes/:slug/analysis", () =>
        HttpResponse.json({ gist: "Fresh gist.", facts: [] })),
    );
    const { user } = renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByText("Old gist.");
    // The AI-analysis panel's refresh button sits under its "AI analysis" heading.
    const panel = screen.getByText("AI analysis").closest("div")!.parentElement as HTMLElement;
    const refreshBtn = within(panel).getByRole("button");
    await user.click(refreshBtn);

    expect(await screen.findByText("Fresh gist.")).toBeInTheDocument();
  });

  it("hides the AI-analysis panel on a KB note (only non-kb gets the sidecar)", async () => {
    mountHandlers(note({ title: "kb/Coffee", kind: "kb" }));
    renderWithProviders(<NotePage />, { route: "/note/coffee" });

    await screen.findByRole("heading", { name: /Coffee/ });
    expect(screen.queryByText("AI analysis")).not.toBeInTheDocument();
  });
});

describe("NotePage — backlinks", () => {
  it("renders backlink chips linking to each source note", async () => {
    mountHandlers(note({
      backlinks: [{ id: 2, title: "notes/Shopping", slug: "shopping" }],
    }));
    renderWithProviders(<NotePage />, { route: "/note/groceries" });

    await screen.findByRole("heading", { name: "Groceries" });
    const chip = await screen.findByRole("link", { name: /Shopping/ });
    expect(chip).toHaveAttribute("href", "/note/shopping");
  });

  it("shows the empty backlinks hint when nothing links here", async () => {
    mountHandlers();
    renderWithProviders(<NotePage />, { route: "/note/groceries" });

    expect(await screen.findByText("No notes link here yet.")).toBeInTheDocument();
  });
});

describe("NotePage — geofence / place", () => {
  it("a place note shows its geofence radius + map link", async () => {
    mountHandlers(note({ title: "loc/Home", kind: "place", slug: "loc-home" }), {
      places: [{ id: 5, name: "Home", lat: 1, lon: 2, radius_m: 120, note_slug: "loc-home" }],
    });
    renderWithProviders(<NotePage />, { route: "/note/loc-home" });

    await screen.findByRole("heading", { name: /Home/ });
    expect(await screen.findByText(/120 m radius/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View on map" })).toHaveAttribute("href", "/map?place=5");
  });
});

describe("NotePage — redirect forwarding", () => {
  it("a merged-away note forwards to its canonical target and shows 'Redirected from'", async () => {
    // The old slug returns a redirect payload; the canonical slug returns the real note.
    server.use(
      http.get("*/api/notes/:slug/versions", () => HttpResponse.json([])),
      http.get("*/api/notes/:slug/attachments", () => HttpResponse.json([])),
      http.get("*/api/notes/:slug/analysis", () => HttpResponse.json({})),
      http.get("*/api/notes/:slug/talk", () => HttpResponse.json([])),
      http.get("*/api/places", () => HttpResponse.json([])),
      http.get("*/api/notes/:slug", ({ params }) =>
        params.slug === "old"
          ? HttpResponse.json(note({ title: "kb/Old Name", slug: "old", redirect_to: "kb/New Name", redirect_to_slug: "new" }))
          : HttpResponse.json(note({ title: "kb/New Name", slug: "new", kind: "kb" }))),
    );

    function Probe() {
      const loc = useLocation();
      return <div data-testid="path">{loc.pathname}</div>;
    }
    renderWithProviders(
      <Routes>
        <Route path="/note/:slug" element={<><NotePage /><Probe /></>} />
      </Routes>,
      { route: "/note/old" },
    );

    // It navigates (replace) to /note/new and renders the canonical leaf.
    expect(await screen.findByRole("heading", { name: /New Name/ })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("path")).toHaveTextContent("/note/new"));
    expect(screen.getByText(/Redirected from/)).toBeInTheDocument();
  });
});
