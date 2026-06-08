// Component-integration (Tier 2) tests for ActionsPage — the action-recipe surface:
// list action definitions grouped by category, open/view a shipped (read-only)
// recipe, edit + save a custom recipe (PUT), create a new one (POST), duplicate a
// shipped recipe into an editable copy, validate a recipe against the server,
// delete a custom recipe, sync-from-repo, and a save error path.
//
// MSW is the only network mock (setup.ts runs onUnhandledRequest: "error"), so every
// endpoint the page touches on mount + per flow is stubbed: GET /api/action-defs,
// GET /api/action-defs/:type, POST /api/action-defs(/validate|/sync), PUT and DELETE
// /api/action-defs/:type. ActionsPage does not consume useAuth(), so no ../App mock
// is needed. window.confirm/alert (used by the delete flow) are stubbed per test.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

import ActionsPage from "./ActionsPage";

// An ActionDef row as GET /api/action-defs returns it. Overridable per test.
function def(overrides: Record<string, unknown> = {}) {
  return {
    type: "create_review",
    source: "repo",
    locked: false,
    category: "Daily",
    num_steps: 3,
    summary: "Create a daily review note.",
    ...overrides,
  };
}

// The detail GET /api/action-defs/:type returns the editable YAML + parsed recipe.
function detail(overrides: Record<string, unknown> = {}) {
  return {
    type: "create_review",
    source: "repo",
    recipe_yaml: "type: create_review\nsteps:\n  - do: create_review\n    with:\n      title: Hello\n",
    recipe: { steps: [{ do: "create_review", with: { title: "Hello" } }] },
    warnings: [] as string[],
    ...overrides,
  };
}

// Register the single GET the page performs on mount. Returns a mutable holder so a
// flow can mutate the list and have the post-action load() return the updated state.
function mountHandlers(list: ReturnType<typeof def>[]) {
  const state = { list };
  server.use(http.get("*/api/action-defs", () => HttpResponse.json(state.list)));
  return state;
}

beforeEach(() => {
  vi.restoreAllMocks();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("ActionsPage", () => {
  it("lists action definitions grouped by category with their badges", async () => {
    mountHandlers([
      def({ type: "create_review", source: "repo", category: "Daily", num_steps: 3 }),
      def({ type: "my_custom", source: "user", category: "Notes", num_steps: 1, locked: true,
            summary: "A hand-rolled recipe." }),
    ]);
    renderWithProviders(<ActionsPage />);

    expect(await screen.findByText("create_review")).toBeInTheDocument();
    expect(screen.getByText("my_custom")).toBeInTheDocument();
    // Category section headers (sorted by CAT_ORDER).
    expect(screen.getByText("Daily")).toBeInTheDocument();
    expect(screen.getByText("Notes")).toBeInTheDocument();
    // Per-row badges: custom + edited markers and the step count.
    expect(screen.getByText("custom")).toBeInTheDocument();
    expect(screen.getByText("edited")).toBeInTheDocument();
    expect(screen.getByText("3 steps")).toBeInTheDocument();
    // A shipped row offers View; a custom row offers Edit + Delete.
    expect(screen.getByRole("button", { name: "View" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("shows the empty state when there are no action definitions", async () => {
    mountHandlers([]);
    renderWithProviders(<ActionsPage />);
    expect(await screen.findByText("No actions.")).toBeInTheDocument();
  });

  it("opens a shipped recipe read-only with its parsed pipeline", async () => {
    mountHandlers([def({ type: "create_review", source: "repo" })]);
    let openedType: string | null = null;
    server.use(
      http.get("*/api/action-defs/:type", ({ params }) => {
        openedType = String(params.type);
        return HttpResponse.json(detail());
      }),
    );
    const { user } = renderWithProviders(<ActionsPage />);

    await screen.findByText("create_review");
    await user.click(screen.getByRole("button", { name: "View" }));

    await waitFor(() => expect(openedType).toBe("create_review"));
    const dialog = await screen.findByRole("dialog");
    // The pipeline visualisation renders from the parsed step tree (the step's `do`
    // appears alongside the same name inside the YAML textarea, hence getAllByText).
    expect(within(dialog).getByText("Pipeline")).toBeInTheDocument();
    expect(within(dialog).getAllByText("create_review").length).toBeGreaterThan(0);
    // The recipe YAML is shown read-only — Save is hidden, the read-only note shows.
    const ta = within(dialog).getByDisplayValue(/type: create_review/) as HTMLTextAreaElement;
    expect(ta.readOnly).toBe(true);
    expect(within(dialog).queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    expect(within(dialog).getByText(/Shipped actions are read-only/)).toBeInTheDocument();
  });

  it("editing a custom recipe saves via PUT with the new YAML and closes the modal", async () => {
    const state = mountHandlers([def({ type: "my_custom", source: "user" })]);
    server.use(
      http.get("*/api/action-defs/:type", () =>
        HttpResponse.json(detail({ type: "my_custom", source: "user" }))),
    );
    let putBody: any = null;
    let putType: string | null = null;
    server.use(
      http.put("*/api/action-defs/:type", async ({ request, params }) => {
        putType = String(params.type);
        putBody = await request.json();
        state.list = [def({ type: "my_custom", source: "user", num_steps: 2 })];
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<ActionsPage />);

    await screen.findByText("my_custom");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog");
    const ta = within(dialog).getByDisplayValue(/type: create_review/) as HTMLTextAreaElement;
    expect(ta.readOnly).toBe(false);
    fireEvent.change(ta, { target: { value: "type: my_custom\nsteps: []\n" } });

    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putType).toBe("my_custom");
    expect(putBody).toEqual({ recipe_yaml: "type: my_custom\nsteps: []\n" });
    // Modal closes after a successful save.
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("creating a New action POSTs /api/action-defs with the seeded recipe YAML", async () => {
    const state = mountHandlers([]);
    let postBody: any = null;
    server.use(
      http.post("*/api/action-defs", async ({ request }) => {
        postBody = await request.json();
        state.list = [def({ type: "my_action", source: "user", num_steps: 1 })];
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<ActionsPage />);

    await screen.findByText("No actions.");
    await user.click(screen.getByRole("button", { name: "+ New" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("New action")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(postBody).not.toBeNull());
    expect(postBody.recipe_yaml).toContain("type: my_action");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // The reloaded list shows the freshly-created custom action.
    expect(await screen.findByText("my_action")).toBeInTheDocument();
  });

  it("duplicating a shipped recipe opens an editable copy seeded with a renamed type", async () => {
    mountHandlers([def({ type: "create_review", source: "repo" })]);
    server.use(http.get("*/api/action-defs/:type", () => HttpResponse.json(detail())));
    const { user } = renderWithProviders(<ActionsPage />);

    await screen.findByText("create_review");
    await user.click(screen.getByRole("button", { name: "Duplicate" }));

    const dialog = await screen.findByRole("dialog");
    // A duplicate is a NEW, editable recipe whose type was renamed to *_copy.
    expect(within(dialog).getByText("New action")).toBeInTheDocument();
    const ta = within(dialog).getByDisplayValue(/type: create_review_copy/) as HTMLTextAreaElement;
    expect(ta.readOnly).toBe(false);
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("Validate POSTs /api/action-defs/validate and surfaces returned warnings", async () => {
    mountHandlers([def({ type: "my_custom", source: "user" })]);
    server.use(
      http.get("*/api/action-defs/:type", () =>
        HttpResponse.json(detail({ type: "my_custom", source: "user" }))),
    );
    let validateBody: any = null;
    server.use(
      http.post("*/api/action-defs/validate", async ({ request }) => {
        validateBody = await request.json();
        return HttpResponse.json({
          recipe: { steps: [{ do: "create_review" }] },
          warnings: ["unused step output 'x'"],
        });
      }),
    );
    const { user } = renderWithProviders(<ActionsPage />);

    await screen.findByText("my_custom");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Validate" }));

    await waitFor(() => expect(validateBody).not.toBeNull());
    expect(validateBody.recipe_yaml).toContain("type: create_review");
    expect(await within(dialog).findByText(/unused step output 'x'/)).toBeInTheDocument();
  });

  it("deleting a custom recipe confirms then DELETEs and reloads", async () => {
    const state = mountHandlers([def({ type: "my_custom", source: "user" })]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let deletedType: string | null = null;
    server.use(
      http.delete("*/api/action-defs/:type", ({ params }) => {
        deletedType = String(params.type);
        state.list = [];
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<ActionsPage />);

    await screen.findByText("my_custom");
    await user.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deletedType).toBe("my_custom"));
    expect(window.confirm).toHaveBeenCalled();
    // After reload the now-empty list shows the empty state.
    expect(await screen.findByText("No actions.")).toBeInTheDocument();
  });

  it("Sync from repo POSTs /sync then reloads the (now larger) list", async () => {
    const state = mountHandlers([def({ type: "create_review", source: "repo" })]);
    let syncPosted = false;
    server.use(
      http.post("*/api/action-defs/sync", () => {
        syncPosted = true;
        state.list = [
          def({ type: "create_review", source: "repo" }),
          def({ type: "freshly_synced", source: "repo", category: "Utility", summary: "New from repo." }),
        ];
        return HttpResponse.json({ synced: 1 });
      }),
    );
    const { user } = renderWithProviders(<ActionsPage />);

    await screen.findByText("create_review");
    await user.click(screen.getByRole("button", { name: "Sync from repo" }));

    await waitFor(() => expect(syncPosted).toBe(true));
    expect(await screen.findByText("freshly_synced")).toBeInTheDocument();
  });

  it("surfaces a save error and keeps the edit modal open", async () => {
    mountHandlers([def({ type: "my_custom", source: "user" })]);
    server.use(
      http.get("*/api/action-defs/:type", () =>
        HttpResponse.json(detail({ type: "my_custom", source: "user" }))),
      http.put("*/api/action-defs/:type", () =>
        HttpResponse.json({ detail: "Recipe rejected: unknown step 'frobnicate'" }, { status: 400 })),
    );
    const { user } = renderWithProviders(<ActionsPage />);

    await screen.findByText("my_custom");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    // The server's detail surfaces in the footer and the editor stays open.
    expect(await within(dialog).findByText(/unknown step 'frobnicate'/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeInTheDocument();
  });
});
