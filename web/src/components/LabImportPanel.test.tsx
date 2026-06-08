// Component-integration (Tier 2) tests for LabImportPanel.tsx — the per-note
// lab-import review surface. The panel lists a note's PDF/image attachments, and for
// each one previews the lab values EXTRACTED from it (a staged table + plot picker)
// before anything reaches the medical Labs trends; the reviewer Approves, Revokes, or
// Re-analyzes per attachment.
//
// Network: every endpoint the panel touches goes through MSW (setup.ts runs
// onUnhandledRequest:"error", so each route is stubbed):
//   GET  /api/notes/:slug/attachments              — the attachment list
//   GET  /api/medical/attachments/:id/labs         — the staged extraction
//   GET  /api/medical/attachments/:id/labs/series  — per-analyte series for the chart
//   POST /api/medical/attachments/:id/labs/approve|revoke|reanalyze
// POSTs are recorded so routing + bodies can be asserted. The "Extract lab values"
// button gates on the llm capability, so the health store is seeded ready in beforeEach
// via ingestVerify (mirrors NotePage.test.tsx) — no production code is touched.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, within, cleanup } from "../test/render";
import { server } from "../test/server";
import { __reset, ingestVerify } from "../health";

// LabImportPanel itself doesn't import useAuth, but a sibling in the tree might; stub
// ../App defensively so the whole auth/router App tree never gets dragged in.
vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

import LabImportPanel from "./LabImportPanel";

const READY_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: false } },
  embeddings: { state: "ready" }, transcription: { state: "ready" },
  push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
};

const SLUG = "labs-2024";
const ATT_ID = 42;

// One staged lab result row, as the server returns it (LabPoint shape + extraction extras).
function row(over: Record<string, unknown> = {}) {
  return {
    t: "2024-03-01", time: null, source: "lab_pdf",
    v: 5.2, vtext: "5.2", value_text: "5.2", unit: "mmol/L", status: "normal", censored: false,
    ref_low: 3.9, ref_high: 5.6, ref_text: "3.9-5.6", flag: null,
    note_id: 1, note_slug: SLUG, note_title: "labs", encounter_id: null,
    analyte_key: "glucose", test_name: "Glucose", collected_at: "2024-03-01",
    collected_time: null, confidence: "high", confidence_reasons: [],
    ...over,
  };
}

// A "needs review" staged extraction with two analytes.
function staged(over: Record<string, unknown> = {}) {
  return {
    status: "staged", results: [row(), row({ analyte_key: "hba1c", test_name: "HbA1c", vtext: "5.4", value_text: "5.4", v: 5.4, unit: "%" })],
    low_confidence: 0, skips: [], identity_state: "" as const, identity: {},
    ...over,
  };
}

const SERIES = {
  analyte: "glucose", test_name: "Glucose", unit: "mmol/L",
  points: [{ t: "2024-03-01", time: null, source: "lab_pdf", v: 5.2, vtext: "5.2", unit: "mmol/L", status: "normal", censored: false, ref_low: 3.9, ref_high: 5.6, ref_text: "3.9-5.6", flag: null, note_id: 1, note_slug: SLUG, note_title: "labs", encounter_id: null }],
  segments: [], domain: { from: "2024-03-01", to: "2024-03-01" }, value_range: { min: 5.2, max: 5.2 },
  encounters: [], other_units: [],
};

// --- recording POST handlers ----------------------------------------------- //
let posted: { url: string; body: any }[] = [];

// Register the GETs every render performs (attachment list + per-attachment labs +
// per-analyte series), with the staged payload overridable per test.
function mountHandlers(labs: Record<string, unknown> | null = staged(),
                       attachments = [{ id: ATT_ID, filename: "labs.pdf", mime: "application/pdf" }]) {
  const state = { labs };
  server.use(
    http.get(`*/api/notes/${SLUG}/attachments`, () => HttpResponse.json(attachments)),
    http.get(`*/api/medical/attachments/${ATT_ID}/labs`, () => HttpResponse.json(state.labs)),
    http.get(`*/api/medical/attachments/${ATT_ID}/labs/series`, () => HttpResponse.json(SERIES)),
    http.post(`*/api/medical/attachments/${ATT_ID}/labs/approve`, async ({ request }) => {
      posted.push({ url: request.url, body: await request.text() });
      state.labs = staged({ status: "approved" });
      return HttpResponse.json({ approved: 2 });
    }),
    http.post(`*/api/medical/attachments/${ATT_ID}/labs/revoke`, async ({ request }) => {
      posted.push({ url: request.url, body: await request.text() });
      state.labs = staged({ status: "staged" });
      return HttpResponse.json({ removed: 2 });
    }),
    http.post(`*/api/medical/attachments/${ATT_ID}/labs/reanalyze`, async ({ request }) => {
      posted.push({ url: request.url, body: await request.text() });
      state.labs = staged({ status: "staged" });
      return HttpResponse.json({ status: "staged", n: 2, analytes: 2 });
    }),
  );
  return state;
}

const approveCalls = () => posted.filter((p) => p.url.includes("/approve"));
const revokeCalls = () => posted.filter((p) => p.url.includes("/revoke"));
const reanalyzeCalls = () => posted.filter((p) => p.url.includes("/reanalyze"));

beforeEach(() => {
  __reset();
  ingestVerify({ capabilities: READY_CAPS });   // llm ready → Extract button enabled
  posted = [];
});
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) listing + staged preview render
// ---------------------------------------------------------------------------- //
describe("LabImportPanel — staged preview", () => {
  it("lists a PDF attachment and renders its staged extraction for review", async () => {
    mountHandlers();
    renderWithProviders(<LabImportPanel slug={SLUG} />);

    // The panel header + the per-attachment "needs review" badge + filename.
    await screen.findByRole("heading", { name: /Lab import/i });
    await screen.findByText(/needs review/i);
    expect(screen.getByText("labs.pdf")).toBeInTheDocument();

    // The summary line reflects the staged counts ("2 results · 2 analytes — review…").
    expect(screen.getByText(/2 results · 2 analytes/i)).toBeInTheDocument();
    expect(screen.getByText(/review before approving/i)).toBeInTheDocument();

    // The two analytes are pickable in the chart selector.
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect(within(select).getByRole("option", { name: /Glucose/ })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: /HbA1c/ })).toBeInTheDocument();

    // Pre-approval: the Approve action is offered, not Revoke.
    expect(screen.getByRole("button", { name: /Approve → add to trends/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Revoke$/i })).not.toBeInTheDocument();
  });

  it("renders nothing when the note has no PDF/image attachments", async () => {
    mountHandlers(null, []);   // empty attachment list
    const { container } = renderWithProviders(<LabImportPanel slug={SLUG} />);
    // No attachments → the panel collapses to null (no heading ever appears).
    await waitFor(() => expect(container.querySelector("h3")).toBeNull());
    expect(screen.queryByText(/Lab import/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) approve commits
// ---------------------------------------------------------------------------- //
describe("LabImportPanel — approve", () => {
  it("Approve posts to the approve endpoint and reflects the approved state", async () => {
    mountHandlers();
    const { user } = renderWithProviders(<LabImportPanel slug={SLUG} />);

    const approve = await screen.findByRole("button", { name: /Approve → add to trends/i });
    await user.click(approve);

    // The approve endpoint fired exactly once with an empty (no-JSON) body.
    await waitFor(() => expect(approveCalls()).toHaveLength(1));
    expect(approveCalls()[0].body).toBe("");

    // After reload the row shows the approved badge and swaps Approve → Revoke.
    await screen.findByText(/approved/i);
    await screen.findByText(/in your trends/i);
    expect(await screen.findByRole("button", { name: /^Revoke$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve → add to trends/i })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (c) revoke (discard from trends)
// ---------------------------------------------------------------------------- //
describe("LabImportPanel — revoke", () => {
  it("Revoke on an approved import posts to revoke and returns it to needs-review", async () => {
    mountHandlers(staged({ status: "approved" }));
    const { user } = renderWithProviders(<LabImportPanel slug={SLUG} />);

    const revoke = await screen.findByRole("button", { name: /^Revoke$/i });
    await user.click(revoke);

    await waitFor(() => expect(revokeCalls()).toHaveLength(1));
    // Back to a stageable state with the Approve action.
    await screen.findByRole("button", { name: /Approve → add to trends/i });
    expect(approveCalls()).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------- //
// (d) re-analyze
// ---------------------------------------------------------------------------- //
describe("LabImportPanel — re-analyze", () => {
  it("Re-analyze posts to the reanalyze endpoint", async () => {
    mountHandlers();
    const { user } = renderWithProviders(<LabImportPanel slug={SLUG} />);

    const reanalyze = await screen.findByRole("button", { name: /^Re-analyze$/i });
    await user.click(reanalyze);

    await waitFor(() => expect(reanalyzeCalls()).toHaveLength(1));
    expect(reanalyzeCalls()[0].url).toContain(`/attachments/${ATT_ID}/labs/reanalyze`);
  });
});

// ---------------------------------------------------------------------------- //
// (e) parse-error / unreadable path
// ---------------------------------------------------------------------------- //
describe("LabImportPanel — unreadable file", () => {
  it("an error-status extraction shows the couldn't-read message and only Re-analyze", async () => {
    mountHandlers(staged({ status: "error" }));
    const { user } = renderWithProviders(<LabImportPanel slug={SLUG} />);

    await screen.findByText(/Couldn’t read lab values from this file/i);
    // The "couldn't read" badge also renders alongside the message.
    expect(screen.getAllByText(/couldn’t read/i).length).toBeGreaterThanOrEqual(1);
    // No staged table / Approve in the error phase — just a Re-analyze affordance.
    expect(screen.queryByRole("button", { name: /Approve → add to trends/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Re-analyze/i }));
    await waitFor(() => expect(reanalyzeCalls()).toHaveLength(1));
  });

  it("renders nothing for an attachment with no extraction yet (null labs)", async () => {
    // labs GET returns null → AttachmentLabs renders null, so the panel has no body rows.
    mountHandlers(null);
    renderWithProviders(<LabImportPanel slug={SLUG} />);
    // Heading still shows (there IS a qualifying attachment), but no badge/table renders.
    await screen.findByRole("heading", { name: /Lab import/i });
    await waitFor(() => expect(screen.queryByText(/needs review/i)).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Approve/i })).not.toBeInTheDocument();
  });
});
