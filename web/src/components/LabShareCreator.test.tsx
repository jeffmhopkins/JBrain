// Component-integration (Tier 2) tests for LabShareCreator.tsx — the OWNER authoring
// surface that mints a scoped, bind-locked lab-share link. The owner ticks exactly
// which analytes to expose, optionally narrows a date window, tunes link settings, and
// hits "Create link" — which POSTs the SCOPED allow-list + window to /api/shares/labs
// and reveals the recipient URL the server returns.
//
// The privacy invariant under test: ONLY the ticked analyte keys ever reach the wire —
// no extra analytes, no names, no notes. We assert the exact create body (allow-list +
// window_from/window_to) and that the revealed URL is the one minted server-side.
//
// No crypto round-trip here: unlike the encrypted-chat link, the lab-share link's URL
// (and any secret) is minted server-side and handed back verbatim — the component never
// touches crypto.subtle, so there is no client-side secret to verify.
//
// Network: the only endpoint the component hits is POST /api/shares/labs, mocked via MSW
// (setup.ts runs onUnhandledRequest:"error"). The available analytes are PASSED AS A PROP
// (the component does not fetch them), so no list endpoint is involved. ../App is stubbed
// defensively so the auth/router App tree is never dragged in (mirrors the sibling tests).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, within, cleanup, fireEvent } from "../test/render";
import { server } from "../test/server";
import type { LabAnalyte } from "../api";

vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

// Imported AFTER the mock factory (vi.mock is hoisted above imports).
import LabShareCreator from "./LabShareCreator";

// A few analytes as the trends store hands them to the creator.
function analyte(over: Partial<LabAnalyte> = {}): LabAnalyte {
  return {
    analyte: "glucose", test_name: "Glucose", unit: "mmol/L",
    n: 3, first_at: "2024-01-01", last_at: "2024-03-01",
    last_value: "5.2", last_status: "normal",
    ...over,
  };
}

const ANALYTES: LabAnalyte[] = [
  analyte(),
  analyte({ analyte: "hba1c", test_name: "HbA1c", unit: "%", last_value: "5.4", last_status: "high" }),
  analyte({ analyte: "ldl", test_name: "LDL Cholesterol", unit: "mg/dL", last_value: "130", last_status: "abnormal" }),
];

// Records the create body so flows can assert the scoped allow-list + window on the wire.
let created: { url: string; body: any }[] = [];

// Register the create endpoint; tests override the response (success / error) as needed.
function createHandler(respond: () => Response) {
  server.use(
    http.post("*/api/shares/labs", async ({ request }) => {
      created.push({ url: request.url, body: await request.json() });
      return respond();
    }),
  );
}

const okResponse = () =>
  HttpResponse.json({ token: "labtok", link_id: 99, url: "https://x/share/labtok" });

beforeEach(() => { created = []; });
afterEach(() => cleanup());

// Open the modal and return the dialog so queries are scoped to it (the trigger button
// stays in the document underneath the portalled Modal).
async function openModal(user: ReturnType<typeof renderWithProviders>["user"]) {
  await user.click(screen.getByRole("button", { name: /Share results…/i }));
  return within(await screen.findByRole("dialog"));
}

// ---------------------------------------------------------------------------- //
// (a) the available analytes (passed as a prop) render for picking
// ---------------------------------------------------------------------------- //
describe("LabShareCreator — analyte list", () => {
  it("renders nothing when there are no analytes to share", () => {
    const { container } = renderWithProviders(<LabShareCreator analytes={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists every available analyte as a tickable option once opened", async () => {
    const { user } = renderWithProviders(<LabShareCreator analytes={ANALYTES} />);
    const dlg = await openModal(user);

    // One checkbox per analyte, each labelled by its test name.
    expect(dlg.getByText("Glucose")).toBeInTheDocument();
    expect(dlg.getByText("HbA1c")).toBeInTheDocument();
    expect(dlg.getByText("LDL Cholesterol")).toBeInTheDocument();
    // One checkbox per analyte (the other checkboxes belong to ShareOptions).
    expect(dlg.getAllByRole("checkbox", { name: /Glucose|HbA1c|Cholesterol/i })).toHaveLength(ANALYTES.length);

    // The counter starts at 0/total and the Create button is disabled until something is ticked.
    expect(dlg.getByText(/0\/3/)).toBeInTheDocument();
    expect(dlg.getByRole("button", { name: /Create link/i })).toBeDisabled();
  });

  it("filters the list by the search box and can select-all the filtered set", async () => {
    const { user } = renderWithProviders(<LabShareCreator analytes={ANALYTES} />);
    const dlg = await openModal(user);

    // NB: the portalled Modal refocuses its dialog on every render (its focus effect
    // depends on a fresh onClose each render), which steals focus from the search box
    // after the first keystroke — so set the query in one change event rather than
    // per-character typing.
    fireEvent.change(dlg.getByPlaceholderText(/Find a result/i), { target: { value: "chol" } });
    // Only the matching analyte survives the filter.
    expect(dlg.getByText("LDL Cholesterol")).toBeInTheDocument();
    expect(dlg.queryByText("Glucose")).not.toBeInTheDocument();
    expect(dlg.queryByText("HbA1c")).not.toBeInTheDocument();
    // Exactly one analyte checkbox remains in the filtered list.
    expect(dlg.getAllByRole("checkbox", { name: /Glucose|HbA1c|Cholesterol/i })).toHaveLength(1);

    // "Select all" ticks the (single) filtered analyte.
    await user.click(dlg.getByRole("button", { name: /Select all/i }));
    expect(dlg.getByText(/1 selected/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) create — the scoped allow-list + window land on the wire, URL is revealed
// ---------------------------------------------------------------------------- //
describe("LabShareCreator — create", () => {
  it("posts ONLY the ticked analytes + the date window, then reveals the minted URL", async () => {
    createHandler(okResponse);
    const { user } = renderWithProviders(<LabShareCreator analytes={ANALYTES} />);
    const dlg = await openModal(user);

    // Tick two of three analytes — LDL is deliberately left OUT.
    await user.click(dlg.getByRole("checkbox", { name: /Glucose/i }));
    await user.click(dlg.getByRole("checkbox", { name: /HbA1c/i }));
    expect(dlg.getByText(/2 selected/)).toBeInTheDocument();

    // Narrow the window.
    const dlgEl = await screen.findByRole("dialog");
    const dates = dlgEl.querySelectorAll('input[type="date"]');
    fireEvent.change(dates[0] as HTMLInputElement, { target: { value: "2024-01-01" } }); // From
    fireEvent.change(dates[1] as HTMLInputElement, { target: { value: "2024-06-01" } }); // To

    await user.click(dlg.getByRole("button", { name: /Create link/i }));

    await waitFor(() => expect(created).toHaveLength(1));
    const body = created[0].body;
    // INVARIANT: exactly the scoped allow-list — no LDL, no extra fields, no names/notes.
    expect(new Set(body.analytes)).toEqual(new Set(["glucose", "hba1c"]));
    expect(body.analytes).toHaveLength(2);
    // The window crossed the wire as the chosen ISO dates.
    expect(body.window_from).toBe("2024-01-01");
    expect(body.window_to).toBe("2024-06-01");
    // PHI defaults: bound to one device, finite expiry.
    expect(body.bind).toBe(true);
    expect(body.ttl_days).toBe(14);

    // The reveal panel shows the SERVER-minted URL verbatim (the component mints no secret).
    const urlInput = await screen.findByDisplayValue("https://x/share/labtok");
    expect(urlInput).toBeInTheDocument();
    await screen.findByText(/Link created/i);
  });

  it("defaults the window to null (full history) when no dates are picked", async () => {
    createHandler(okResponse);
    const { user } = renderWithProviders(<LabShareCreator analytes={ANALYTES} />);
    const dlg = await openModal(user);

    await user.click(dlg.getByRole("checkbox", { name: /Glucose/i }));
    await user.click(dlg.getByRole("button", { name: /Create link/i }));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0].body.analytes).toEqual(["glucose"]);
    // Empty date fields → explicit null window (the whole history is in scope).
    expect(created[0].body.window_from).toBeNull();
    expect(created[0].body.window_to).toBeNull();
  });

  it("copies the minted link to the clipboard from the reveal panel", async () => {
    createHandler(okResponse);
    const { user } = renderWithProviders(<LabShareCreator analytes={ANALYTES} />);
    const dlg = await openModal(user);

    await user.click(dlg.getByRole("checkbox", { name: /Glucose/i }));
    await user.click(dlg.getByRole("button", { name: /Create link/i }));
    await screen.findByDisplayValue("https://x/share/labtok");

    // user-event installs its own clipboard stub; read the written value back through it.
    await user.click(screen.getByRole("button", { name: /Copy link/i }));
    await waitFor(async () =>
      expect(await navigator.clipboard.readText()).toBe("https://x/share/labtok"));
  });
});

// ---------------------------------------------------------------------------- //
// (c) validation — an empty selection cannot create a link
// ---------------------------------------------------------------------------- //
describe("LabShareCreator — validation", () => {
  it("blocks create with no analytes ticked (button disabled, nothing on the wire)", async () => {
    // No create handler registered: if a request fired, onUnhandledRequest:"error" trips it.
    const { user } = renderWithProviders(<LabShareCreator analytes={ANALYTES} />);
    const dlg = await openModal(user);

    // The Create button is disabled while the selection is empty, so no POST is possible.
    expect(dlg.getByRole("button", { name: /Create link/i })).toBeDisabled();

    // Ticking then un-ticking returns to the blocked state.
    const glucose = dlg.getByRole("checkbox", { name: /Glucose/i });
    await user.click(glucose);
    expect(dlg.getByRole("button", { name: /Create link/i })).toBeEnabled();
    await user.click(glucose);
    expect(dlg.getByRole("button", { name: /Create link/i })).toBeDisabled();

    expect(created).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------- //
// (d) error path — a failed create surfaces the message, no URL revealed
// ---------------------------------------------------------------------------- //
describe("LabShareCreator — error", () => {
  it("shows the server error and stays on the form (no link revealed)", async () => {
    createHandler(() =>
      HttpResponse.json({ detail: "Too many active links" }, { status: 400 }));
    const { user } = renderWithProviders(<LabShareCreator analytes={ANALYTES} />);
    const dlg = await openModal(user);

    await user.click(dlg.getByRole("checkbox", { name: /Glucose/i }));
    await user.click(dlg.getByRole("button", { name: /Create link/i }));

    // The request fired ...
    await waitFor(() => expect(created).toHaveLength(1));
    // ... and the error detail is surfaced; the form (not the reveal panel) is still shown.
    await screen.findByText(/Too many active links/i);
    expect(screen.queryByText(/Link created/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create link/i })).toBeInTheDocument();
  });
});
