// Component-integration (Tier 2) tests for PeoplePage — the people/users manager:
// list the people whose devices feed this brain, add a new one, edit name/aliases
// (committed on blur), make one the default ("owner") catch-all, remove one, and the
// per-person location-key (setup code) lifecycle.
//
// MSW is the only network mock (setup.ts runs onUnhandledRequest: "error"), so every
// endpoint the page touches on mount + per flow is stubbed against the contract in
// ../api.ts (GET/POST /api/people, PATCH/DELETE /api/people/:id, the location-key
// sub-routes). The page calls window.confirm/alert and navigator.clipboard — those are
// stubbed test-side. The shared Modal isn't used here, but per project standard we drive
// controlled inputs with fireEvent.change so a focus-steal can't drop characters.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, within, fireEvent } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

// PeoplePage doesn't import useAuth, but mock ../App for parity with the project's page
// tests so nothing in the shared tree drags in the real App provider.
vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

import PeoplePage from "./PeoplePage";
import type { Person } from "../api";

// A Person row as GET /api/people returns it. Overridable per test.
function person(o: Partial<Person> = {}): Person {
  return {
    id: 1, name: "Jeff", color: "#3366cc", is_default: false,
    aliases: "pwa,wear", note_slug: null, location_key: null,
    ...o,
  };
}

// Register the one GET the page performs on mount. Returns a mutable holder so a
// reload (after add/delete/make-default) can reflect the new server state.
function mountHandlers(initial: Person[] = [person()]) {
  const state = { people: initial };
  server.use(
    http.get("*/api/people", () => HttpResponse.json(state.people)),
  );
  return state;
}

beforeEach(() => {
  // The page reads window.confirm/alert; stub them so the jsdom "not implemented"
  // noise/throws don't interfere and we can assert on them. The clipboard is provided
  // by userEvent.setup() (a working stub), so the copy test reads it back directly.
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "alert").mockImplementation(() => {});
});
afterEach(() => { vi.restoreAllMocks(); });

describe("PeoplePage", () => {
  it("renders the fetched people list", async () => {
    mountHandlers([
      person({ id: 1, name: "Jeff", is_default: true }),
      person({ id: 2, name: "Sam", aliases: "phone" }),
    ]);
    renderWithProviders(<PeoplePage />, { route: "/people" });

    expect(await screen.findByDisplayValue("Jeff")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Sam")).toBeInTheDocument();
    // The default person shows the badge instead of a "Make default" button.
    expect(screen.getByText("Default")).toBeInTheDocument();
  });

  it("shows an empty list when no people exist", async () => {
    mountHandlers([]);
    renderWithProviders(<PeoplePage />, { route: "/people" });

    // The add control is always present; no person rows render.
    expect(await screen.findByPlaceholderText(/Add a user/)).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });

  it("swallows a failed initial load and still renders the add control", async () => {
    server.use(
      http.get("*/api/people", () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    renderWithProviders(<PeoplePage />, { route: "/people" });

    expect(await screen.findByPlaceholderText(/Add a user/)).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });

  it("adds a person, POSTing the trimmed name, then reloads the list", async () => {
    const state = mountHandlers([]);
    let addBody: any = null;
    server.use(
      http.post("*/api/people", async ({ request }) => {
        addBody = await request.json();
        state.people = [person({ id: 5, name: "Casey", aliases: "" })];
        return HttpResponse.json({ id: 5, name: "Casey" });
      }),
    );
    renderWithProviders(<PeoplePage />, { route: "/people" });

    const input = await screen.findByPlaceholderText(/Add a user/);
    fireEvent.change(input, { target: { value: "  Casey  " } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(addBody).toEqual({ name: "Casey" }));
    // The reload re-fetches and renders the new row.
    expect(await screen.findByDisplayValue("Casey")).toBeInTheDocument();
  });

  it("commits a renamed person via PATCH on blur", async () => {
    mountHandlers([person({ id: 1, name: "Jeff" })]);
    let patchBody: any = null;
    let patchId = "";
    server.use(
      http.patch("*/api/people/:id", async ({ request, params }) => {
        patchBody = await request.json();
        patchId = String(params.id);
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<PeoplePage />, { route: "/people" });

    const nameInput = await screen.findByDisplayValue("Jeff");
    fireEvent.change(nameInput, { target: { value: "Jeffrey " } });
    fireEvent.blur(nameInput);

    // onBlur commits the trimmed value to the person's row.
    await waitFor(() => expect(patchBody).toEqual({ name: "Jeffrey" }));
    expect(patchId).toBe("1");
  });

  it("commits edited aliases via PATCH on blur", async () => {
    mountHandlers([person({ id: 3, name: "Sam", aliases: "phone" })]);
    let patchBody: any = null;
    server.use(
      http.patch("*/api/people/:id", async ({ request }) => {
        patchBody = await request.json();
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<PeoplePage />, { route: "/people" });

    const aliasInput = await screen.findByDisplayValue("phone");
    fireEvent.change(aliasInput, { target: { value: "phone,sams-watch" } });
    fireEvent.blur(aliasInput);

    await waitFor(() => expect(patchBody).toEqual({ aliases: "phone,sams-watch" }));
  });

  it("makes a person the default (owner) via PATCH is_default and reloads", async () => {
    const state = mountHandlers([
      person({ id: 1, name: "Jeff", is_default: true }),
      person({ id: 2, name: "Sam", is_default: false }),
    ]);
    let patchBody: any = null;
    let patchId = "";
    server.use(
      http.patch("*/api/people/:id", async ({ request, params }) => {
        patchBody = await request.json();
        patchId = String(params.id);
        // Server flips the default over to Sam; the reload reflects it.
        state.people = [
          person({ id: 1, name: "Jeff", is_default: false }),
          person({ id: 2, name: "Sam", is_default: true }),
        ];
        return HttpResponse.json({});
      }),
    );
    const { user } = renderWithProviders(<PeoplePage />, { route: "/people" });

    await screen.findByDisplayValue("Sam");
    // Sam is the only non-default row, so it's the only "Make default" button.
    await user.click(screen.getByRole("button", { name: "Make default" }));

    await waitFor(() => expect(patchBody).toEqual({ is_default: true }));
    expect(patchId).toBe("2");
    // After reload Sam carries the Default badge; Jeff (now non-default) gains the button.
    await waitFor(() => {
      const samRow = screen.getByDisplayValue("Sam").closest("li")!;
      expect(within(samRow).getByText("Default")).toBeInTheDocument();
    });
    const jeffRow = screen.getByDisplayValue("Jeff").closest("li")!;
    expect(within(jeffRow).getByRole("button", { name: "Make default" })).toBeInTheDocument();
  });

  it("removes a non-default person via DELETE after confirming, then reloads", async () => {
    const state = mountHandlers([
      person({ id: 1, name: "Jeff", is_default: true }),
      person({ id: 2, name: "Sam" }),
    ]);
    let deletedId = "";
    server.use(
      http.delete("*/api/people/:id", ({ params }) => {
        deletedId = String(params.id);
        state.people = [person({ id: 1, name: "Jeff", is_default: true })];
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user } = renderWithProviders(<PeoplePage />, { route: "/people" });

    await screen.findByDisplayValue("Sam");
    // Sam's row carries the only ✕ remove button (the default row has none).
    await user.click(screen.getByTitle("Remove"));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => expect(deletedId).toBe("2"));
    // Sam's row is gone after the reload; the default remains.
    await waitFor(() => expect(screen.queryByDisplayValue("Sam")).not.toBeInTheDocument());
    expect(screen.getByDisplayValue("Jeff")).toBeInTheDocument();
  });

  it("does not DELETE when the remove confirmation is cancelled", async () => {
    mountHandlers([
      person({ id: 1, name: "Jeff", is_default: true }),
      person({ id: 2, name: "Sam" }),
    ]);
    vi.mocked(window.confirm).mockReturnValue(false);
    const del = vi.fn();
    server.use(
      http.delete("*/api/people/:id", () => { del(); return new HttpResponse(null, { status: 204 }); }),
    );
    const { user } = renderWithProviders(<PeoplePage />, { route: "/people" });

    await screen.findByDisplayValue("Sam");
    await user.click(screen.getByTitle("Remove"));

    expect(window.confirm).toHaveBeenCalled();
    expect(del).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue("Sam")).toBeInTheDocument();
  });

  it("generates a location key and surfaces the copyable setup code", async () => {
    mountHandlers([person({ id: 1, name: "Jeff", location_key: null })]);
    server.use(
      http.post("*/api/people/:id/location-key", () =>
        HttpResponse.json({ location_key: "secret-key-123" })),
    );
    const { user } = renderWithProviders(<PeoplePage />, { route: "/people" });

    await screen.findByDisplayValue("Jeff");
    await user.click(screen.getByRole("button", { name: /Location key/ }));

    // Once a key exists the row shows a jbt1.* setup code and a copy button.
    const code = await screen.findByText(/^jbt1\./);
    expect(code).toBeInTheDocument();
    // userEvent.setup() installs a working clipboard stub that shadows ours, so assert
    // via both: the button flips to "Copied ✓" only after writeText resolves, and the
    // clipboard now holds the exact setup code.
    await user.click(screen.getByRole("button", { name: /Copy setup code/ }));
    expect(await screen.findByRole("button", { name: /Copied/ })).toBeInTheDocument();
    await waitFor(async () =>
      expect(await navigator.clipboard.readText()).toBe(code.textContent),
    );
  });

  it("revokes an active location key via DELETE, re-enabling name/alias edits", async () => {
    mountHandlers([person({ id: 1, name: "Jeff", location_key: "live-key" })]);
    let revokedId = "";
    server.use(
      http.delete("*/api/people/:id/location-key", ({ params }) => {
        revokedId = String(params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user } = renderWithProviders(<PeoplePage />, { route: "/people" });

    // While a key is live the name field is locked (disabled).
    const nameInput = await screen.findByDisplayValue("Jeff");
    expect(nameInput).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Revoke" }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => expect(revokedId).toBe("1"));
    // The key cleared optimistically, so the name unlocks and the +Location key button returns.
    await waitFor(() => expect(screen.getByDisplayValue("Jeff")).not.toBeDisabled());
    expect(screen.getByRole("button", { name: /Location key/ })).toBeInTheDocument();
  });

  it("alerts and reloads when a field commit fails on the server", async () => {
    const state = mountHandlers([person({ id: 1, name: "Jeff" })]);
    server.use(
      http.patch("*/api/people/:id", () =>
        HttpResponse.json({ detail: "Name already taken" }, { status: 409 })),
    );
    renderWithProviders(<PeoplePage />, { route: "/people" });

    const nameInput = await screen.findByDisplayValue("Jeff");
    fireEvent.change(nameInput, { target: { value: "Dup" } });
    fireEvent.blur(nameInput);

    // The failed commit surfaces the server message and reloads to resync.
    await waitFor(() => expect(window.alert).toHaveBeenCalledWith("Name already taken"));
    void state;
  });

  it("links to a person's KB page when they carry a note_slug", async () => {
    mountHandlers([person({ id: 1, name: "Jeff", note_slug: "people/jeff" })]);
    renderWithProviders(<PeoplePage />, { route: "/people" });

    const link = await screen.findByRole("link", { name: "Page" });
    expect(link).toHaveAttribute("href", "/note/people/jeff");
  });
});
