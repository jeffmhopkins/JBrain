// Component-integration (Tier 2) tests for EntitiesPage — the entity browser:
// list/search entities, view an entity's detail, and the durable identity controls
// (merge a duplicate in, split a wrong match apart, add/remove an alias).
//
// MSW is the only network mock (setup.ts runs onUnhandledRequest: "error"), so every
// endpoint the page touches on mount + per flow is stubbed. Mutating ops call
// /api/entities/status before the op and then poll it on a background interval; a
// permissive status handler keeps that poll from tripping the unhandled-request guard.
// The page reads useCapability("embeddings") from the health store (seeded ready) and
// useSearchParams from the router (driven via the test route).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import { __reset, ingestVerify } from "../health";
import { READY_CAPS } from "../test/handlers";

// EntitiesPage doesn't import useAuth, but other shared modules in the tree might; mock
// ../App for parity with the project's page tests so nothing drags in the App provider.
vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

import EntitiesPage from "./EntitiesPage";

// An EntitySummary as the list endpoint returns it.
function summary(o: Partial<Record<string, unknown>> = {}) {
  return { id: 1, type: "person", canonical_name: "Jeff Hopkins", note_count: 3, article_title: null, ...o };
}

// A full EntityDetail as /api/entities/:id (and merge/alias) return it.
function detail(o: Partial<Record<string, unknown>> = {}) {
  return {
    id: 1, type: "person", canonical_name: "Jeff Hopkins", note_count: 3, article_title: null,
    normalized_key: "jeff hopkins", aliases: [],
    notes: [{ id: 10, title: "notes/Standup", slug: "standup", created_at: "2024-01-01T00:00:00Z" }],
    ...o,
  };
}

const STATUS_IDLE = { rebuilding: false, status: "idle", generation: 1, last_error: null };

// Register the GETs the page performs on mount/selection plus the rebuild-status poll.
// Returns a record of the latest detail so post-op re-fetches reflect the change.
function mountHandlers(list = [summary()], det = detail()) {
  const state = { detail: det };
  server.use(
    http.get("*/api/entities/status", () => HttpResponse.json(STATUS_IDLE)),
    http.get("*/api/entities/:id/decisions", () => HttpResponse.json([])),
    http.get("*/api/entities/:id", () => HttpResponse.json(state.detail)),
    http.get("*/api/entities", () => HttpResponse.json(list)),
  );
  return state;
}

beforeEach(() => {
  __reset();
  ingestVerify({ capabilities: READY_CAPS }); // seed health so useCapability("embeddings") is ready
});
afterEach(() => { vi.restoreAllMocks(); });

describe("EntitiesPage", () => {
  it("renders the fetched entity list", async () => {
    mountHandlers([
      summary({ id: 1, canonical_name: "Jeff Hopkins", note_count: 3 }),
      summary({ id: 2, type: "org", canonical_name: "Acme Corp", note_count: 1 }),
    ]);
    renderWithProviders(<EntitiesPage />, { route: "/entities" });

    expect(await screen.findByRole("button", { name: /Jeff Hopkins/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Acme Corp/ })).toBeInTheDocument();
  });

  it("issues a type-and-query-scoped list request when searching", async () => {
    let listUrl = "";
    server.use(
      http.get("*/api/entities/status", () => HttpResponse.json(STATUS_IDLE)),
      http.get("*/api/entities/:id/decisions", () => HttpResponse.json([])),
      http.get("*/api/entities/:id", () => HttpResponse.json(detail())),
      http.get("*/api/entities", ({ request }) => {
        listUrl = request.url;
        return HttpResponse.json([summary()]);
      }),
    );
    // q + type come straight from the URL search params the page reads.
    renderWithProviders(<EntitiesPage />, { route: "/entities?q=jeff&type=person" });

    await screen.findByRole("button", { name: /Jeff Hopkins/ });
    await waitFor(() => expect(listUrl).toContain("q=jeff"));
    expect(listUrl).toContain("type=person");
  });

  it("loads an entity's detail when a row is clicked", async () => {
    mountHandlers([summary()], detail({
      article_title: "kb/Jeff Hopkins",
      notes: [{ id: 10, title: "notes/Standup", slug: "standup", created_at: "2024-01-01T00:00:00Z" }],
    }));
    const { user } = renderWithProviders(<EntitiesPage />, { route: "/entities" });

    await user.click(await screen.findByRole("button", { name: /Jeff Hopkins/ }));

    // Detail pane shows the heading, the KB article link, and the mentions list.
    expect(await screen.findByRole("heading", { name: /Jeff Hopkins/ })).toBeInTheDocument();
    expect(await screen.findByText(/Mentioned in 1 note/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Standup/ })).toBeInTheDocument();
  });

  it("auto-opens the matching detail when the URL carries a query", async () => {
    mountHandlers([summary()], detail());
    renderWithProviders(<EntitiesPage />, { route: "/entities?q=Jeff Hopkins" });

    // A q that matches a single/exact list entity auto-selects it on load.
    expect(await screen.findByRole("heading", { name: /Jeff Hopkins/ })).toBeInTheDocument();
    expect(await screen.findByText(/Mentioned in/)).toBeInTheDocument();
  });

  it("merging a picked duplicate POSTs source/into and reflects the canonical detail", async () => {
    mountHandlers([summary()], detail());
    let mergeBody: any = null;
    server.use(
      // EntityPicker's inline search returns the duplicate to fold in.
      http.get("*/api/entities", ({ request }) => {
        const q = new URL(request.url).searchParams.get("q");
        if (q) return HttpResponse.json([summary({ id: 2, canonical_name: "Jeffrey H", note_count: 1 })]);
        return HttpResponse.json([summary()]);
      }),
      http.post("*/api/entities/merge", async ({ request }) => {
        mergeBody = await request.json();
        return HttpResponse.json(detail({ canonical_name: "Jeff Hopkins", note_count: 4, aliases: ["Jeffrey H"] }));
      }),
    );
    const { user } = renderWithProviders(<EntitiesPage />, { route: "/entities" });

    await user.click(await screen.findByRole("button", { name: /Jeff Hopkins/ }));
    await screen.findByRole("heading", { name: /Jeff Hopkins/ });

    // Type into the merge picker, then pick the returned duplicate.
    const mergeInput = await screen.findByPlaceholderText(/duplicate to merge in/);
    await user.type(mergeInput, "jeff");
    await user.click(await screen.findByRole("button", { name: /Jeffrey H/ }));

    // The op fires merge(source=2, into=1) and the page absorbs the returned canonical detail.
    await waitFor(() => expect(mergeBody).toEqual({ source_id: 2, into_id: 1 }));
    expect(await screen.findByText(/a\.k\.a\. Jeffrey H/)).toBeInTheDocument();
  });

  it("splitting a picked entity POSTs a_id/b_id", async () => {
    mountHandlers([summary()], detail());
    let splitBody: any = null;
    server.use(
      http.get("*/api/entities", ({ request }) => {
        const q = new URL(request.url).searchParams.get("q");
        if (q) return HttpResponse.json([summary({ id: 7, canonical_name: "Other Jeff", note_count: 2 })]);
        return HttpResponse.json([summary()]);
      }),
      http.post("*/api/entities/split", async ({ request }) => {
        splitBody = await request.json();
        return HttpResponse.json({ ok: true, rebuilding: true });
      }),
    );
    const { user } = renderWithProviders(<EntitiesPage />, { route: "/entities" });

    await user.click(await screen.findByRole("button", { name: /Jeff Hopkins/ }));
    await screen.findByRole("heading", { name: /Jeff Hopkins/ });

    const splitInput = await screen.findByPlaceholderText(/keep separate/);
    await user.type(splitInput, "other");
    await user.click(await screen.findByRole("button", { name: /Other Jeff/ }));

    // split(a=selected=1, b=picked=7).
    await waitFor(() => expect(splitBody).toEqual({ a_id: 1, b_id: 7 }));
  });

  it("adding an alias POSTs the display to the entity's aliases endpoint", async () => {
    mountHandlers([summary()], detail());
    let aliasBody: any = null;
    let aliasPath = "";
    server.use(
      http.post("*/api/entities/:id/aliases", async ({ request, params }) => {
        aliasBody = await request.json();
        aliasPath = String(params.id);
        return HttpResponse.json(detail({ aliases: ["Jeff H"] }));
      }),
    );
    const { user } = renderWithProviders(<EntitiesPage />, { route: "/entities" });

    await user.click(await screen.findByRole("button", { name: /Jeff Hopkins/ }));
    await screen.findByRole("heading", { name: /Jeff Hopkins/ });

    const aliasInput = await screen.findByPlaceholderText(/Jeff Hopkins/);
    await user.type(aliasInput, "Jeff H");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(aliasBody).toEqual({ display: "Jeff H" }));
    expect(aliasPath).toBe("1");
    expect(await screen.findByText(/a\.k\.a\. Jeff H/)).toBeInTheDocument();
  });

  it("surfaces an error when an identity op fails and keeps the detail open", async () => {
    mountHandlers([summary()], detail());
    server.use(
      http.post("*/api/entities/:id/aliases", () =>
        HttpResponse.json({ detail: "Alias rejected by server" }, { status: 500 })),
    );
    const { user } = renderWithProviders(<EntitiesPage />, { route: "/entities" });

    await user.click(await screen.findByRole("button", { name: /Jeff Hopkins/ }));
    await screen.findByRole("heading", { name: /Jeff Hopkins/ });

    const aliasInput = await screen.findByPlaceholderText(/Jeff Hopkins/);
    await user.type(aliasInput, "Bad Alias");
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByText(/Alias rejected by server/)).toBeInTheDocument();
    // The identity card stays mounted so the failed change isn't lost.
    expect(within(screen.getByText("Identity").closest(".card")!).getByRole("button", { name: "Add" }))
      .toBeInTheDocument();
  });

  it("shows the empty-state when no entities exist", async () => {
    mountHandlers([]);
    renderWithProviders(<EntitiesPage />, { route: "/entities" });

    expect(await screen.findByText(/No entities yet/)).toBeInTheDocument();
  });
});
