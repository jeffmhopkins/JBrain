// Component-integration (Tier 2) tests for LinkAuditPanel.tsx — the in-wiki
// maintenance panel that scans GET /api/notes/links/audit on mount, lists
// [[Target|Display]] links whose shortened label names a different article than the
// target, and offers per-finding Fix (POST .../fix) plus Fix-all (POST .../fix-all).
//
// Network is MSW only (setup.ts runs onUnhandledRequest: "error"), so every endpoint
// the panel touches is stubbed via server.use(...); default status/auth handlers cover
// the rest. Mutating endpoints record their request so routing + body are asserted.
// jsdom has no native alert()/confirm(), so both are stubbed.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, within, cleanup } from "../test/render";
import { server } from "../test/server";
import type { LinkAuditFinding } from "../api";

import LinkAuditPanel from "./LinkAuditPanel";

// Two findings as GET /api/notes/links/audit returns them.
const F1: LinkAuditFinding = {
  source_id: 10, source_title: "notes/Recipes", source_slug: "recipes",
  raw: "[[notes/Sourdough|Bread]]", target: "notes/Sourdough", target_title: "notes/Sourdough",
  target_slug: "sourdough", display: "Bread", desired_display: "Sourdough",
  fixed: "[[notes/Sourdough|Sourdough]]", resolved_title: "notes/Bread", resolved_slug: "bread",
  reason: "label-mismatch",
};
const F2: LinkAuditFinding = {
  source_id: 11, source_title: "notes/Trips", source_slug: "trips",
  raw: "[[notes/Paris|Capital]]", target: "notes/Paris", target_title: "notes/Paris",
  target_slug: "paris", display: "Capital", desired_display: "Paris",
  fixed: "[[notes/Paris|Paris]]", resolved_title: null, resolved_slug: null,
  reason: "label-mismatch",
};

// --- recording mutation handlers ------------------------------------------- //
let posted: { url: string; body: any }[] = [];

function auditGet(findings: LinkAuditFinding[]) {
  return http.get("*/api/notes/links/audit", () => HttpResponse.json({ findings }));
}
function recordFix(response: any = { fixed: true }, status = 200) {
  return http.post("*/api/notes/links/audit/fix", async ({ request }) => {
    let body: any = null;
    try { body = await request.json(); } catch { /* no body */ }
    posted.push({ url: request.url, body });
    return HttpResponse.json(response, { status });
  });
}
function recordFixAll(response: any = { fixed: 2 }, status = 200) {
  return http.post("*/api/notes/links/audit/fix-all", async ({ request }) => {
    posted.push({ url: request.url, body: null });
    return HttpResponse.json(response, { status });
  });
}

beforeEach(() => {
  posted = [];
  vi.stubGlobal("alert", vi.fn());
  vi.stubGlobal("confirm", vi.fn(() => true));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const fixCalls = () => posted.filter((p) => p.url.includes("/audit/fix") && !p.url.includes("fix-all"));
const fixAllCalls = () => posted.filter((p) => p.url.includes("/audit/fix-all"));

// ---------------------------------------------------------------------------- //
// (a) the audit scan + list rendering
// ---------------------------------------------------------------------------- //
describe("LinkAuditPanel — scan + list", () => {
  it("scans on mount, auto-expands, badges the count, and renders one row per finding", async () => {
    server.use(auditGet([F1, F2]));
    renderWithProviders(<LinkAuditPanel />);

    // Findings>0 ⇒ the panel auto-opens and shows the descriptive blurb.
    await screen.findByText(/Links whose shortened label names a different article/i);
    // Count badge.
    expect(screen.getByText("2")).toBeInTheDocument();

    // Each finding describes "shows X but links to Y" — one per row.
    expect(screen.getAllByText(/but links to/i)).toHaveLength(2);
    expect(screen.getAllByText("Bread").length).toBeGreaterThan(0);
    expect(screen.getByText("Capital")).toBeInTheDocument();
    // leaf() strips the notes/ root for the target titles.
    expect(screen.getAllByText("Sourdough").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Paris").length).toBeGreaterThan(0);
    // The "Open note" / source links point at the source slug.
    const links = screen.getAllByRole("link", { name: /Recipes|Open note/i });
    expect(links.some((a) => a.getAttribute("href") === "/note/recipes")).toBe(true);
    // The proposed fixed form is shown in a code element.
    expect(screen.getByText("[[notes/Sourdough|Sourdough]]")).toBeInTheDocument();
    // F1 has a resolved_title → the "is actually" clause renders.
    expect(screen.getByText(/is actually/i)).toBeInTheDocument();
  });

  it("renders the clean/empty state when the audit returns no findings", async () => {
    server.use(auditGet([]));
    renderWithProviders(<LinkAuditPanel />);

    // Collapsed header shows a ✓ clean marker (no auto-open when empty).
    await screen.findByText(/✓ clean/i);
    expect(screen.queryByRole("button", { name: /Fix all/i })).not.toBeInTheDocument();

    // Expanding by clicking the header reveals the "no mismatches" copy.
    await screen.getByText(/Link label audit/i).click();
    await screen.findByText(/No mismatched link labels/i);
  });

  it("swallows a failing audit and shows no findings", async () => {
    server.use(
      http.get("*/api/notes/links/audit", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    renderWithProviders(<LinkAuditPanel />);

    // scan()'s catch sets findings=[] and state="done" → clean marker, no crash, no badge.
    await screen.findByText(/✓ clean/i);
    expect(screen.queryByText(/but links to/i)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) Fix one
// ---------------------------------------------------------------------------- //
describe("LinkAuditPanel — fix one", () => {
  it("posts the finding to /audit/fix with note_id/target/display and removes the row", async () => {
    server.use(auditGet([F1, F2]), recordFix());
    const { user } = renderWithProviders(<LinkAuditPanel />);
    await screen.findByText(/Links whose shortened label/i);

    // Click the F1 row's Fix button (scoped to its item to avoid F2's).
    const row = screen.getByText("[[notes/Sourdough|Sourdough]]").closest(".link-audit-item")!;
    await user.click(within(row as HTMLElement).getByRole("button", { name: /^Fix$/ }));

    await waitFor(() => expect(fixCalls()).toHaveLength(1));
    // Body carries note_id (source_id), target, and the *current* display.
    expect(fixCalls()[0].body).toEqual({ note_id: 10, target: "notes/Sourdough", display: "Bread" });
    // The fixed row disappears; the other finding stays.
    await waitFor(() =>
      expect(screen.queryByText("[[notes/Sourdough|Sourdough]]")).not.toBeInTheDocument());
    expect(screen.getByText("[[notes/Paris|Paris]]")).toBeInTheDocument();
  });

  it("alerts and keeps the row when the fix request fails", async () => {
    const alertFn = vi.fn();
    vi.stubGlobal("alert", alertFn);
    server.use(auditGet([F1]), recordFix({ detail: "fix refused" }, 409));
    const { user } = renderWithProviders(<LinkAuditPanel />);
    await screen.findByText(/Links whose shortened label/i);

    await user.click(screen.getByRole("button", { name: /^Fix$/ }));

    await waitFor(() => expect(fixCalls()).toHaveLength(1));
    await waitFor(() => expect(alertFn).toHaveBeenCalledWith("fix refused"));
    // The row survives the failure.
    expect(screen.getByText("[[notes/Sourdough|Sourdough]]")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (c) Fix all + Rescan
// ---------------------------------------------------------------------------- //
describe("LinkAuditPanel — fix all / rescan", () => {
  it("Fix all confirms, posts to /audit/fix-all, then rescans (list clears)", async () => {
    // A tiny state machine: the post-fix-all rescan GET returns no findings.
    const state = { findings: [F1, F2] as LinkAuditFinding[] };
    server.use(
      http.get("*/api/notes/links/audit", () => HttpResponse.json({ findings: state.findings })),
      http.post("*/api/notes/links/audit/fix-all", () => {
        posted.push({ url: "/api/notes/links/audit/fix-all", body: null });
        state.findings = []; // server fixed them all
        return HttpResponse.json({ fixed: 2 });
      }),
    );
    const { user } = renderWithProviders(<LinkAuditPanel />);
    await screen.findByText(/Links whose shortened label/i);

    await user.click(screen.getByRole("button", { name: /Fix all \(2\)/i }));

    expect((globalThis.confirm as any)).toHaveBeenCalled();
    await waitFor(() => expect(fixAllCalls()).toHaveLength(1));
    // The rescan picked up the now-empty list → clean state.
    await waitFor(() => expect(screen.getByText(/No mismatched link labels/i)).toBeInTheDocument());
  });

  it("does nothing when Fix all is cancelled at the confirm", async () => {
    vi.stubGlobal("confirm", vi.fn(() => false));
    server.use(auditGet([F1, F2]), recordFixAll());
    const { user } = renderWithProviders(<LinkAuditPanel />);
    await screen.findByText(/Links whose shortened label/i);

    await user.click(screen.getByRole("button", { name: /Fix all \(2\)/i }));

    // Confirm returned false → no request, rows remain.
    expect(fixAllCalls()).toHaveLength(0);
    expect(screen.getByText("[[notes/Sourdough|Sourdough]]")).toBeInTheDocument();
  });

  it("Rescan re-fetches the audit on demand", async () => {
    let hits = 0;
    server.use(
      http.get("*/api/notes/links/audit", () => {
        hits += 1;
        return HttpResponse.json({ findings: [F1] });
      }),
    );
    const { user } = renderWithProviders(<LinkAuditPanel />);
    await screen.findByText(/Links whose shortened label/i);
    expect(hits).toBe(1); // mount scan

    await user.click(screen.getByRole("button", { name: /Rescan/i }));
    await waitFor(() => expect(hits).toBe(2));
  });
});
