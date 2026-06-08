// Component-integration (Tier 2) tests for PromptsPanel — the in-app prompt editor:
// it loads the grouped, filterable prompt list (with an "overridden" badge on
// customised rows), opens a prompt into a Modal editor seeded with the effective
// text, saves an edit via PUT /api/prompts/:key { value }, resets a customised
// prompt to the shipped default via DELETE /api/prompts/:key, and renders the
// empty / no-match state.
//
// MSW is the only network mock (setup.ts runs onUnhandledRequest: "error"), so every
// endpoint the panel touches on mount + per flow is stubbed: GET /api/prompts plus
// per-flow PUT and DELETE /api/prompts/:key. PromptsPanel does not consume useAuth(),
// so no ../App mock is needed. The Modal editor uses controlled inputs/textareas and
// the shared Modal steals focus per-render, so edits go through fireEvent.change.
// window.confirm (used by reset + the discard guard) is stubbed per test.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderWithProviders, screen, waitFor, within } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

import PromptsPanel from "./PromptsPanel";

// A PromptRow as GET /api/prompts returns it. Overridable per test.
function row(overrides: Record<string, unknown> = {}) {
  const def = "Shipped default text.";
  return {
    key: "tools.search",
    default: def,
    override: null as string | null,
    effective: def,
    ...overrides,
  };
}

// Register the single GET the panel performs on mount + after each mutation.
// Returns a mutable holder so a flow can mutate the list and have the post-action
// load() return the updated state.
function mountHandlers(list: ReturnType<typeof row>[]) {
  const state = { list };
  server.use(http.get("*/api/prompts", () => HttpResponse.json(state.list)));
  return state;
}

beforeEach(() => {
  vi.restoreAllMocks();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("PromptsPanel", () => {
  it("loads and renders prompts grouped, marking customised rows with an overridden badge", async () => {
    mountHandlers([
      // A modes.* row: friendlyLabel uses the mode name (parts[1]); Modes group is open by default.
      row({ key: "modes.daily.system", default: "Be terse.", effective: "Be terse.", override: null }),
      // A customised tools.* row → "overridden" badge.
      row({ key: "tools.search", default: "Search the KB.", effective: "Search the KB. (edited)", override: "Search the KB. (edited)" }),
    ]);
    renderWithProviders(<PromptsPanel />);

    // Modes group is expanded by default, so its row label is visible.
    expect(await screen.findByText("Daily")).toBeInTheDocument();
    // Group section headers render for the non-empty groups.
    expect(screen.getByText("Modes")).toBeInTheDocument();
    expect(screen.getByText("Tools")).toBeInTheDocument();

    // The Tools group is collapsed by default, so its row isn't shown until expanded.
    expect(screen.queryByText("Search")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Tools"));

    expect(await screen.findByText("Search")).toBeInTheDocument();
    // The customised row carries the "overridden" badge; the un-overridden one does not.
    expect(screen.getByText("overridden")).toBeInTheDocument();
    // The raw key is shown as a hint under each label.
    expect(screen.getByText("tools.search")).toBeInTheDocument();
  });

  it("a filter force-opens collapsed groups and narrows the list", async () => {
    mountHandlers([
      row({ key: "modes.daily.system", default: "Be terse.", effective: "Be terse." }),
      row({ key: "tools.search", default: "Search the KB.", effective: "Search the KB." }),
    ]);
    renderWithProviders(<PromptsPanel />);

    await screen.findByText("Daily");
    // Typing a filter that only matches the (collapsed) Tools row force-opens it
    // and hides the non-matching Modes row.
    fireEvent.change(screen.getByPlaceholderText("Filter prompts…"), { target: { value: "search" } });

    expect(await screen.findByText("Search")).toBeInTheDocument();
    expect(screen.queryByText("Daily")).not.toBeInTheDocument();
    expect(screen.queryByText("Modes")).not.toBeInTheDocument();
  });

  it("opens a prompt into the editor seeded with the effective text", async () => {
    mountHandlers([row({ key: "modes.daily.system", default: "Be terse.", effective: "Speak plainly." })]);
    renderWithProviders(<PromptsPanel />);

    fireEvent.click(await screen.findByText("Daily"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Daily")).toBeInTheDocument();
    // The editor textarea is seeded with the effective value.
    expect(within(dialog).getByDisplayValue("Speak plainly.")).toBeInTheDocument();
    // Save is disabled until the draft diverges from the effective value.
    expect(within(dialog).getByRole("button", { name: "Save" })).toBeDisabled();
    // The shipped default is shown in the collapsible details section.
    expect(within(dialog).getByText("Shipped default")).toBeInTheDocument();
  });

  it("editing a prompt and saving PUTs /api/prompts/:key with the new value then closes + reloads", async () => {
    const state = mountHandlers([row({ key: "tools.search", default: "Search.", effective: "Search.", override: null })]);
    let putBody: any = null;
    let putKey: string | null = null;
    server.use(
      http.put("*/api/prompts/:key", async ({ request, params }) => {
        putKey = String(params.key);
        putBody = await request.json();
        state.list = [row({ key: "tools.search", default: "Search.", effective: "Search harder.", override: "Search harder." })];
        return HttpResponse.json({ ok: true });
      }),
    );
    renderWithProviders(<PromptsPanel />);

    // Tools is collapsed by default — expand, then open the row.
    fireEvent.click(await screen.findByText("Tools"));
    fireEvent.click(await screen.findByText("Search"));

    const dialog = await screen.findByRole("dialog");
    const ta = within(dialog).getByDisplayValue("Search.") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "Search harder." } });

    const save = within(dialog).getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putKey).toBe("tools.search");
    expect(putBody).toEqual({ value: "Search harder." });
    // Modal closes after a successful save.
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("resetting a customised prompt confirms then DELETEs /api/prompts/:key and reloads the default", async () => {
    const state = mountHandlers([
      row({ key: "tools.search", default: "Search.", effective: "Search harder.", override: "Search harder." }),
    ]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let deletedKey: string | null = null;
    server.use(
      http.delete("*/api/prompts/:key", ({ params }) => {
        deletedKey = String(params.key);
        // After reset the override is cleared and the effective text reverts to the default.
        state.list = [row({ key: "tools.search", default: "Search.", effective: "Search.", override: null })];
        return HttpResponse.json({ ok: true });
      }),
    );
    renderWithProviders(<PromptsPanel />);

    fireEvent.click(await screen.findByText("Tools"));
    fireEvent.click(await screen.findByText("Search"));

    const dialog = await screen.findByRole("dialog");
    // The reset button only renders on an overridden prompt.
    fireEvent.click(within(dialog).getByRole("button", { name: "Reset to default" }));

    await waitFor(() => expect(deletedKey).toBe("tools.search"));
    expect(window.confirm).toHaveBeenCalled();
    // Modal closes and the now-default row no longer shows the overridden badge.
    // (The Tools group stays expanded from earlier, so the reloaded row is still visible.)
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await screen.findByText("Search");
    await waitFor(() => expect(screen.queryByText("overridden")).not.toBeInTheDocument());
  });

  it("a non-overridden prompt offers no Reset to default action", async () => {
    mountHandlers([row({ key: "modes.daily.system", default: "Be terse.", effective: "Be terse.", override: null })]);
    renderWithProviders(<PromptsPanel />);

    fireEvent.click(await screen.findByText("Daily"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("button", { name: "Reset to default" })).not.toBeInTheDocument();
  });

  it("shows the no-match empty state when a filter matches nothing", async () => {
    mountHandlers([row({ key: "tools.search", default: "Search.", effective: "Search." })]);
    renderWithProviders(<PromptsPanel />);

    fireEvent.change(await screen.findByPlaceholderText("Filter prompts…"), { target: { value: "zzzznope" } });
    expect(await screen.findByText("No matching prompts.")).toBeInTheDocument();
  });

  it("renders the empty state when the prompt list is empty", async () => {
    mountHandlers([]);
    renderWithProviders(<PromptsPanel />);
    expect(await screen.findByText("No matching prompts.")).toBeInTheDocument();
  });

  it("the Modified toggle shows only customised prompts", async () => {
    mountHandlers([
      row({ key: "modes.daily.system", default: "Be terse.", effective: "Be terse.", override: null }),
      row({ key: "modes.weekly.system", default: "Recap.", effective: "Recap weekly.", override: "Recap weekly." }),
    ]);
    renderWithProviders(<PromptsPanel />);

    // Both modes rows visible by default (Modes group is open).
    expect(await screen.findByText("Daily")).toBeInTheDocument();
    expect(screen.getByText("Weekly")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Modified" }));
    // Only the overridden one survives the filter.
    await waitFor(() => expect(screen.queryByText("Daily")).not.toBeInTheDocument());
    expect(screen.getByText("Weekly")).toBeInTheDocument();
  });
});
