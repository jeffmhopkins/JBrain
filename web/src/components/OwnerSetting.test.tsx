// Component (Tier 2) tests for OwnerSetting — the "Owner" identity card. On mount it
// GETs /api/people/owner and seeds the name + aliases inputs; Save PUTs the trimmed
// values via /api/people/owner and shows the re-analyze confirmation. Save is gated by
// a dirty check (blank name, or values unchanged from the loaded owner, keep it
// disabled); a failed save surfaces the server's error message and re-loads.
//
// Network: MSW only (setup.ts runs onUnhandledRequest:"error"), so the owner GET and
// the save PUT are stubbed via server.use(...). Controlled inputs steal focus under
// user-event typing, so edits go through fireEvent.change to stay deterministic.
import { afterEach, describe, expect, it } from "vitest";
import { renderWithProviders, screen, waitFor, fireEvent } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import OwnerSetting from "./OwnerSetting";

const OWNER = { id: 1, name: "Jeff", display: "Jeff", aliases: "JMH", is_set: true };

afterEach(() => { server.resetHandlers(); });

describe("OwnerSetting", () => {
  it("loads the owner and populates the name + aliases inputs", async () => {
    server.use(http.get("*/api/people/owner", () => HttpResponse.json(OWNER)));
    renderWithProviders(<OwnerSetting />);

    const nameInput = (await screen.findByPlaceholderText("e.g. Jeff Hopkins")) as HTMLInputElement;
    await waitFor(() => expect(nameInput.value).toBe("Jeff"));
    expect((screen.getByPlaceholderText("e.g. Jeff, JMH") as HTMLInputElement).value).toBe("JMH");
    // Loaded + set owner: the kb/People path shows the name, no "not set yet" note.
    expect(screen.getByText(/kb\/People\/Jeff/)).toBeInTheDocument();
  });

  it("shows the unset fallback copy when the owner name isn't set yet", async () => {
    server.use(http.get("*/api/people/owner", () =>
      HttpResponse.json({ id: null, name: "Owner", display: "Owner", aliases: "", is_set: false })));
    renderWithProviders(<OwnerSetting />);

    expect(await screen.findByText(/It isn.t set yet/)).toBeInTheDocument();
    // Unset → the name input is seeded blank (not the generic "Owner").
    const nameInput = screen.getByPlaceholderText("e.g. Jeff Hopkins") as HTMLInputElement;
    await waitFor(() => expect(nameInput.value).toBe(""));
  });

  it("Save is disabled until the name is non-empty and actually changed", async () => {
    server.use(http.get("*/api/people/owner", () => HttpResponse.json(OWNER)));
    renderWithProviders(<OwnerSetting />);

    const nameInput = (await screen.findByPlaceholderText("e.g. Jeff Hopkins")) as HTMLInputElement;
    await waitFor(() => expect(nameInput.value).toBe("Jeff"));
    const saveBtn = screen.getByRole("button", { name: "Save" });
    // Loaded unchanged → not dirty → disabled.
    expect(saveBtn).toBeDisabled();

    // Blanking the name keeps it disabled (no nameless owner).
    fireEvent.change(nameInput, { target: { value: "   " } });
    expect(saveBtn).toBeDisabled();

    // A real change enables it.
    fireEvent.change(nameInput, { target: { value: "Jeff Hopkins" } });
    expect(saveBtn).toBeEnabled();
  });

  it("saving PUTs the trimmed name + aliases and shows the re-analyze confirmation", async () => {
    server.use(http.get("*/api/people/owner", () => HttpResponse.json(OWNER)));
    let putBody: any = null;
    server.use(
      http.put("*/api/people/owner", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json({ id: 1, name: "Jeff Hopkins", display: "Jeff Hopkins", aliases: "Jeff", is_set: true });
      }),
    );
    renderWithProviders(<OwnerSetting />);

    const nameInput = (await screen.findByPlaceholderText("e.g. Jeff Hopkins")) as HTMLInputElement;
    await waitFor(() => expect(nameInput.value).toBe("Jeff"));
    fireEvent.change(nameInput, { target: { value: "  Jeff Hopkins  " } });
    fireEvent.change(screen.getByPlaceholderText("e.g. Jeff, JMH"), { target: { value: " Jeff " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBody).toEqual({ name: "Jeff Hopkins", aliases: "Jeff" }));
    expect(await screen.findByText(/Re-analyze your notes/)).toBeInTheDocument();
    // The saved server value re-seeds the input.
    await waitFor(() => expect(nameInput.value).toBe("Jeff Hopkins"));
  });

  it("surfaces the server error message when the save fails", async () => {
    server.use(http.get("*/api/people/owner", () => HttpResponse.json(OWNER)));
    server.use(
      http.put("*/api/people/owner", () =>
        HttpResponse.json({ detail: "Name already taken" }, { status: 400 })),
    );
    renderWithProviders(<OwnerSetting />);

    const nameInput = (await screen.findByPlaceholderText("e.g. Jeff Hopkins")) as HTMLInputElement;
    await waitFor(() => expect(nameInput.value).toBe("Jeff"));
    fireEvent.change(nameInput, { target: { value: "Taken Name" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Name already taken")).toBeInTheDocument();
  });
});
