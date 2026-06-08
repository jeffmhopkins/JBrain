// Component-integration (Tier 2) tests for MedicalPage.tsx — the Medical-mode admin
// surface opened from Advanced. The page curates the destination picklist that the
// composer's medical mode files notes under (notes/medical/<destination>/) and the
// "owner identity" used to guard imported lab PDFs against a DOB mismatch.
//
// Network: every endpoint the page touches goes through MSW (setup.ts runs
// onUnhandledRequest:"error", so each route is stubbed):
//   GET /api/medical/destinations  — the destination list
//   PUT /api/medical/destinations  — save (add/remove) the list
//   GET /api/medical/owner         — the owner name/dob
//   PUT /api/medical/owner         — save the owner identity
// PUT bodies are recorded so routing + payloads can be asserted. MedicalPage doesn't
// import useAuth, but ../App is stubbed defensively (mirrors the sibling page tests) so
// the whole auth/router App tree is never dragged in. No production code is touched.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, within, cleanup } from "../test/render";
import { server } from "../test/server";

vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

import MedicalPage from "./MedicalPage";

// --- recording PUT handlers ------------------------------------------------- //
let putDests: any[] = [];
let putOwner: any[] = [];

// Register the two GETs the page performs on mount, plus the two PUTs the save
// actions issue. The list + owner are kept in module-level state so a post-save
// refetch returns the persisted value. Each is overridable per test.
function mountHandlers(
  names: string[] = ["2026-03 Admission", "Cardiology"],
  owner: { name?: string; dob?: string } = { name: "Jeff", dob: "1980-01-02" },
) {
  const state = { names, owner };
  server.use(
    http.get("*/api/medical/destinations", () => HttpResponse.json({ names: state.names })),
    http.put("*/api/medical/destinations", async ({ request }) => {
      const body: any = await request.json();
      putDests.push(body);
      state.names = body.names;
      return HttpResponse.json({ names: state.names });
    }),
    http.get("*/api/medical/owner", () => HttpResponse.json(state.owner)),
    http.put("*/api/medical/owner", async ({ request }) => {
      const body: any = await request.json();
      putOwner.push(body);
      state.owner = { name: body.name, dob: body.dob };
      return HttpResponse.json(state.owner);
    }),
  );
  return state;
}

beforeEach(() => {
  putDests = [];
  putOwner = [];
});
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) initial load
// ---------------------------------------------------------------------------- //
describe("MedicalPage — load", () => {
  it("loads and renders the destination list and the owner identity fields", async () => {
    mountHandlers();
    renderWithProviders(<MedicalPage />);

    // Each fetched destination shows as a row with its notes/medical/<name>/ path.
    expect(await screen.findByText("2026-03 Admission")).toBeInTheDocument();
    expect(screen.getByText("Cardiology")).toBeInTheDocument();
    expect(screen.getByText("notes/medical/Cardiology/")).toBeInTheDocument();

    // The owner section renders with the fetched name/dob seeded into the inputs.
    expect(screen.getByRole("heading", { name: /Whose labs are these/i })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByPlaceholderText(/Name \(as printed/i)).toHaveValue("Jeff"));
    expect(screen.getByPlaceholderText(/DOB/i)).toHaveValue("1980-01-02");
  });

  it("shows the empty-state message when there are no destinations", async () => {
    mountHandlers([], {});
    renderWithProviders(<MedicalPage />);

    expect(await screen.findByText(/No destinations yet — add one above\./i)).toBeInTheDocument();
    // Owner inputs come up blank when the owner GET returns an empty object.
    expect(screen.getByPlaceholderText(/Name \(as printed/i)).toHaveValue("");
  });

  it("survives a failed destinations fetch (caught) and still renders the page", async () => {
    server.use(
      http.get("*/api/medical/destinations", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 })),
      http.get("*/api/medical/owner", () => HttpResponse.json({})),
    );
    renderWithProviders(<MedicalPage />);

    // The .catch(() => {}) swallows the error: no rows, but the page chrome is intact.
    expect(await screen.findByRole("heading", { name: /Whose labs are these/i })).toBeInTheDocument();
    expect(screen.getByText(/No destinations yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) adding a destination
// ---------------------------------------------------------------------------- //
describe("MedicalPage — add destination", () => {
  it("Add PUTs the appended list and renders the new row", async () => {
    mountHandlers(["Cardiology"]);
    const { user } = renderWithProviders(<MedicalPage />);

    await screen.findByText("Cardiology");
    const input = screen.getByPlaceholderText(/Add a destination/i);
    await user.type(input, "Neurology");
    await user.click(screen.getByRole("button", { name: "Add" }));

    // The PUT carries the existing entry plus the new one, in order.
    await waitFor(() => expect(putDests).toHaveLength(1));
    expect(putDests[0]).toEqual({ names: ["Cardiology", "Neurology"] });

    // The refetched list shows the added row and the input is cleared.
    expect(await screen.findByText("Neurology")).toBeInTheDocument();
    expect(input).toHaveValue("");
  });

  it("does not PUT when the new-destination input is empty/whitespace", async () => {
    mountHandlers(["Cardiology"]);
    const { user } = renderWithProviders(<MedicalPage />);

    await screen.findByText("Cardiology");
    const addBtn = screen.getByRole("button", { name: "Add" });
    // Button is disabled while the trimmed name is empty.
    expect(addBtn).toBeDisabled();

    // Typing only spaces keeps it disabled, and Enter is a no-op.
    await user.type(screen.getByPlaceholderText(/Add a destination/i), "   ");
    expect(addBtn).toBeDisabled();
    await user.keyboard("{Enter}");
    expect(putDests).toHaveLength(0);
  });

  it("adds via the Enter key in the input", async () => {
    mountHandlers([]);
    const { user } = renderWithProviders(<MedicalPage />);

    await screen.findByText(/No destinations yet/i);
    await user.type(screen.getByPlaceholderText(/Add a destination/i), "ICU{Enter}");

    await waitFor(() => expect(putDests).toHaveLength(1));
    expect(putDests[0]).toEqual({ names: ["ICU"] });
    expect(await screen.findByText("ICU")).toBeInTheDocument();
  });

  it("surfaces an alert and leaves the list unchanged when the save fails", async () => {
    mountHandlers(["Cardiology"]);
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    server.use(
      http.put("*/api/medical/destinations", () =>
        HttpResponse.json({ detail: "Disk full" }, { status: 500 })),
    );
    const { user } = renderWithProviders(<MedicalPage />);

    await screen.findByText("Cardiology");
    await user.type(screen.getByPlaceholderText(/Add a destination/i), "Neurology");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("Disk full"));
    // The failed entry never lands in the list.
    expect(screen.queryByText("Neurology")).not.toBeInTheDocument();
    expect(screen.getByText("Cardiology")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (c) removing a destination
// ---------------------------------------------------------------------------- //
describe("MedicalPage — remove destination", () => {
  it("Remove (confirmed) PUTs the filtered list and drops the row", async () => {
    mountHandlers(["Cardiology", "Neurology"]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = renderWithProviders(<MedicalPage />);

    const row = (await screen.findByText("Cardiology")).closest("li")!;
    await user.click(within(row).getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(putDests).toHaveLength(1));
    expect(putDests[0]).toEqual({ names: ["Neurology"] });
    await waitFor(() => expect(screen.queryByText("Cardiology")).not.toBeInTheDocument());
    expect(screen.getByText("Neurology")).toBeInTheDocument();
  });

  it("Remove (cancelled at confirm) issues no PUT and keeps the row", async () => {
    mountHandlers(["Cardiology"]);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { user } = renderWithProviders(<MedicalPage />);

    const row = (await screen.findByText("Cardiology")).closest("li")!;
    await user.click(within(row).getByRole("button", { name: "Remove" }));

    expect(putDests).toHaveLength(0);
    expect(screen.getByText("Cardiology")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (d) owner identity save
// ---------------------------------------------------------------------------- //
describe("MedicalPage — owner identity", () => {
  it("Save PUTs the trimmed name/dob and flashes a Saved confirmation", async () => {
    mountHandlers(["Cardiology"], { name: "", dob: "" });
    const { user } = renderWithProviders(<MedicalPage />);

    await screen.findByText("Cardiology");
    const name = screen.getByPlaceholderText(/Name \(as printed/i);
    const dob = screen.getByPlaceholderText(/DOB/i);
    await user.type(name, "  Jane Roe  ");
    await user.type(dob, "1990-05-06");
    await user.click(screen.getByRole("button", { name: "Save" }));

    // Body is whitespace-trimmed by saveOwner().
    await waitFor(() => expect(putOwner).toHaveLength(1));
    expect(putOwner[0]).toEqual({ name: "Jane Roe", dob: "1990-05-06" });
    // The button label flips to the saved confirmation.
    expect(await screen.findByRole("button", { name: /Saved/i })).toBeInTheDocument();
  });

  it("surfaces an alert when the owner save fails", async () => {
    mountHandlers(["Cardiology"], { name: "", dob: "" });
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    server.use(
      http.put("*/api/medical/owner", () =>
        HttpResponse.json({ detail: "Owner locked" }, { status: 409 })),
    );
    const { user } = renderWithProviders(<MedicalPage />);

    await screen.findByText("Cardiology");
    await user.type(screen.getByPlaceholderText(/Name \(as printed/i), "Jane");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("Owner locked"));
    // No saved confirmation on failure.
    expect(screen.queryByRole("button", { name: /Saved/i })).not.toBeInTheDocument();
  });
});
