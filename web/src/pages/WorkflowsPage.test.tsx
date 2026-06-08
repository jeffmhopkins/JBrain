// Component-integration (Tier 2) tests for WorkflowsPage — the workflow/automation
// editor: list triggers, enable/disable, edit + save config, run-now (with a live
// status poll), sync-from-repo, reset-to-repo, and an error path.
//
// MSW is the only network mock (setup.ts runs onUnhandledRequest: "error"), so every
// endpoint the page touches on mount + per flow is stubbed: GET /api/workflows,
// GET /api/workflows/action-types, plus per-action toggle/run/run-status/runs/sync/
// reset/PUT. The page reads useAuth() from ../App (only `appTz`); stub the module so
// we don't drag the whole auth/router App tree in just for the timezone.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

import WorkflowsPage from "./WorkflowsPage";

// A complete Workflow row as the server returns it. Overridable per test.
function wf(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    name: "Nightly digest",
    trigger_type: "schedule",
    trigger_config: { cron: "0 3 * * *" },
    action_type: "append_to_note",
    action_config: { title: "Notes", text: "hello" },
    enabled: true,
    locked: false,
    source: "repo",
    last_status: null as string | null,
    last_run_at: null as string | null,
    ...overrides,
  };
}

const ACTION_TYPES = [
  { type: "append_to_note", category: "Notes",
    config: [{ key: "title", label: "Note title", type: "text" },
             { key: "text", label: "Body text", type: "textarea" }] },
  { type: "notify", category: "Utility", config: [] },
];

// Register the two GETs every render performs on mount: the workflow list and the
// action-type catalog. Returns a mutable holder so a flow can mutate the list and
// have the post-action load() return the updated state.
function mountHandlers(list: ReturnType<typeof wf>[]) {
  const state = { list };
  server.use(
    http.get("*/api/workflows", () => HttpResponse.json(state.list)),
    http.get("*/api/workflows/action-types", () => HttpResponse.json(ACTION_TYPES)),
  );
  return state;
}

beforeEach(() => {
  vi.restoreAllMocks();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("WorkflowsPage", () => {
  it("lists workflows grouped by category with their enabled state", async () => {
    mountHandlers([
      wf({ id: 1, name: "Nightly digest", enabled: true, action_type: "append_to_note" }),
      wf({ id: 2, name: "Ping me", enabled: false, action_type: "notify", trigger_type: "event" }),
    ]);
    renderWithProviders(<WorkflowsPage />);

    expect(await screen.findByText("Nightly digest")).toBeInTheDocument();
    expect(screen.getByText("Ping me")).toBeInTheDocument();
    // Category section headers come from the action-type catalog.
    expect(screen.getByText("Notes")).toBeInTheDocument();
    expect(screen.getByText("Utility")).toBeInTheDocument();
    // Enabled state shows as on/off badges and the toggle button label flips.
    expect(screen.getByText("on")).toBeInTheDocument();
    expect(screen.getByText("off")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disable" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable" })).toBeInTheDocument();
  });

  it("toggling enable/disable POSTs the workflow's toggle endpoint and reflects the new state", async () => {
    const state = mountHandlers([wf({ id: 7, name: "Nightly digest", enabled: true })]);
    let toggledId: string | null = null;
    server.use(
      http.post("*/api/workflows/:id/toggle", ({ params }) => {
        toggledId = String(params.id);
        // Persist so the post-toggle load() returns the flipped row.
        state.list = [wf({ id: 7, name: "Nightly digest", enabled: false })];
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<WorkflowsPage />);

    await screen.findByText("Nightly digest");
    await user.click(screen.getByRole("button", { name: "Disable" }));

    await waitFor(() => expect(toggledId).toBe("7"));
    // After reload the row is now off and offers to Enable.
    expect(await screen.findByRole("button", { name: "Enable" })).toBeInTheDocument();
    expect(screen.getByText("off")).toBeInTheDocument();
  });

  it("editing a workflow's config saves via PUT with the new payload", async () => {
    const state = mountHandlers([
      wf({ id: 3, name: "Nightly digest", action_config: { title: "Notes", text: "hello" } }),
    ]);
    let putBody: any = null;
    let putId: string | null = null;
    server.use(
      http.put("*/api/workflows/:id", async ({ request, params }) => {
        putId = String(params.id);
        putBody = await request.json();
        state.list = [wf({ id: 3, name: "Nightly digest", action_config: { title: putBody.action_config?.title } })];
        return HttpResponse.json({ id: 3 });
      }),
    );
    const { user } = renderWithProviders(<WorkflowsPage />);

    await screen.findByText("Nightly digest");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    // The edit modal opens seeded from the row.
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Edit trigger")).toBeInTheDocument();

    // Edit the schema-driven "Note title" field — ConfigFields mutates the same
    // action_config JSON the page serializes into the PUT body.
    // Set the whole value in one change. ConfigFields re-parses the JSON string and
    // re-derives this input's value on every keystroke, so character-by-character
    // typing churns the controlled value; a single fireEvent.change is deterministic.
    const title = within(dialog).getByDisplayValue("Notes") as HTMLInputElement;
    fireEvent.change(title, { target: { value: "Journal" } });

    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putId).toBe("3");
    expect(putBody).toEqual({
      name: "Nightly digest",
      trigger_type: "schedule",
      trigger_config: { cron: "0 3 * * *" },
      action_type: "append_to_note",
      action_config: { title: "Journal", text: "hello" },
      enabled: true,
    });
    // Modal closes after a successful save.
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("Run now POSTs the run endpoint then polls run-status and reflects the result", async () => {
    const state = mountHandlers([wf({ id: 5, name: "Nightly digest" })]);
    let runPosted = false;
    server.use(
      http.post("*/api/workflows/:id/run", () => { runPosted = true; return HttpResponse.json({ ok: true }); }),
      // One poll: already finished ok, with a friendly detail.
      http.get("*/api/workflows/:id/run-status", () =>
        HttpResponse.json({ status: "ok", detail: "Appended 3 notes", events: ["write_note"] })),
      http.get("*/api/workflows/:id/runs", () =>
        HttpResponse.json([{ id: 1, started_at: "2024-01-01T00:00:00Z", status: "ok", detail: "Appended 3 notes" }])),
    );
    void state;
    const { user } = renderWithProviders(<WorkflowsPage />);

    await screen.findByText("Nightly digest");
    await user.click(screen.getByRole("button", { name: "Run now" }));

    await waitFor(() => expect(runPosted).toBe(true));
    // The result detail surfaces once the poll leaves "running" (appears in the
    // card status line and the live-watch modal).
    expect(await screen.findAllByText("Appended 3 notes")).not.toHaveLength(0);
  });

  it("Sync from repo POSTs /sync and surfaces the synced count", async () => {
    const state = mountHandlers([wf({ id: 1, name: "Nightly digest" })]);
    let syncPosted = false;
    server.use(
      http.post("*/api/workflows/sync", () => {
        syncPosted = true;
        state.list = [wf({ id: 1, name: "Nightly digest" }), wf({ id: 2, name: "Freshly synced" })];
        return HttpResponse.json({ synced: 2 });
      }),
    );
    const { user } = renderWithProviders(<WorkflowsPage />);

    await screen.findByText("Nightly digest");
    await user.click(screen.getByRole("button", { name: "Sync from repo" }));

    await waitFor(() => expect(syncPosted).toBe(true));
    expect(await screen.findByText("Synced 2 workflow(s) from repo.")).toBeInTheDocument();
    // The reloaded list includes the newly-synced workflow.
    expect(await screen.findByText("Freshly synced")).toBeInTheDocument();
  });

  it("Reset to repo POSTs /reset for a locked workflow and drops the lock", async () => {
    const state = mountHandlers([
      wf({ id: 9, name: "Edited workflow", locked: true }),
    ]);
    let resetId: string | null = null;
    server.use(
      http.post("*/api/workflows/:id/reset", ({ params }) => {
        resetId = String(params.id);
        state.list = [wf({ id: 9, name: "Edited workflow", locked: false })];
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<WorkflowsPage />);

    await screen.findByText("Edited workflow");
    // The locked badge + the reset control only appear for a locked row.
    expect(screen.getByText("locked")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset to repo" }));

    await waitFor(() => expect(resetId).toBe("9"));
    // After reload the lock (and its reset button) are gone.
    await waitFor(() => expect(screen.queryByText("locked")).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Reset to repo" })).not.toBeInTheDocument();
  });

  it("surfaces a save error and keeps the edit modal open", async () => {
    mountHandlers([wf({ id: 4, name: "Nightly digest" })]);
    server.use(
      http.put("*/api/workflows/:id", () =>
        HttpResponse.json({ detail: "Action config rejected by server" }, { status: 400 })),
    );
    const { user } = renderWithProviders(<WorkflowsPage />);

    await screen.findByText("Nightly digest");
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    // The server's detail is shown and the editor stays open (Save still present).
    // The error renders both at the top of the page and in the modal footer.
    expect(await within(dialog).findByText("Action config rejected by server")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeInTheDocument();
  });
});
