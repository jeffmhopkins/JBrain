// Component-integration (Tier 2) tests for LabsPage — the labs overview: it lists the
// analytes a user has on file, charts the selected one's trend (delegating to <LabChart>),
// remembers the last-viewed analyte + time window in localStorage, links pending lab
// imports for review, and hosts the share-results authoring modal (<LabShareCreator>).
//
// Network: MSW is the only network mock (setup.ts runs onUnhandledRequest:"error"), so every
// endpoint the page touches on mount is stubbed. On mount LabsPage hits three GETs:
//   /api/medical/labs/pending      (the import-review banner)
//   /api/medical/labs/analytes     (the picker list)
//   /api/medical/labs/series       (the selected analyte's series — drives <LabChart>)
// The picker auto-selects the first analyte (or the restored one), so the series GET always
// fires; tests assert the analyte it asked for. <LabChart> is pure SVG (no fetch of its own),
// so returning a series with points renders it inline — no child stubbing needed.
//
// Auth: LabsPage doesn't read useAuth (only api helpers), so no ../App mock is required.
// setAccessKey() gives the api() bearer header so requests look authenticated.
//
// Determinism: queries are by role/label/text; the chart window is fully determined by the
// series.domain we return — no wall-clock dependence — so no fake timers / no time pin.
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Route, Routes } from "react-router-dom";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import type { LabAnalyte, LabSeries, LabPoint } from "../api";
import { setAccessKey } from "../api";

import LabsPage from "./LabsPage";

// ---- builders -------------------------------------------------------------- //
function analyte(o: Partial<LabAnalyte> & { analyte: string; test_name: string }): LabAnalyte {
  return {
    unit: "mg/dL",
    n: 3,
    first_at: "2024-01-01",
    last_at: "2026-01-01",
    last_value: "100",
    last_status: "normal",
    ...o,
  };
}

function pt(o: Partial<LabPoint> & { t: string; v: number | null }): LabPoint {
  return {
    time: null, source: null,
    vtext: o.v == null ? "" : String(o.v),
    unit: "mg/dL", status: "normal", censored: false,
    ref_low: null, ref_high: null, ref_text: null, flag: null,
    note_id: null, note_slug: null, note_title: null, encounter_id: null,
    ...o,
  } as LabPoint;
}

function series(o: Partial<LabSeries> & { analyte: string; test_name: string; points: LabPoint[] }): LabSeries {
  const ts = o.points.map((p) => p.t).sort();
  return {
    unit: "mg/dL",
    segments: [], encounters: [], other_units: [], value_range: null,
    domain: o.points.length ? { from: ts[0], to: ts[ts.length - 1] } : null,
    ...o,
  } as LabSeries;
}

// Default analytes list — two results, glucose abnormal-recent so it sorts first.
const GLUCOSE = analyte({ analyte: "glucose", test_name: "Glucose", last_value: "142", last_status: "high", last_at: "2026-05-01" });
const SODIUM = analyte({ analyte: "sodium", test_name: "Sodium", unit: "mmol/L", last_value: "140", last_status: "normal", last_at: "2026-04-01" });

// A multi-point series so <LabChart> actually draws (>=2 non-censored points).
const GLUCOSE_SERIES = series({
  analyte: "glucose", test_name: "Glucose", unit: "mg/dL",
  points: [pt({ t: "2025-02-01", v: 95 }), pt({ t: "2026-05-01", v: 142, status: "high" })],
});
const SODIUM_SERIES = series({
  analyte: "sodium", test_name: "Sodium", unit: "mmol/L",
  points: [pt({ t: "2025-03-01", v: 138 }), pt({ t: "2026-04-01", v: 140 })],
});

// Register the three mount GETs. `seen` records which analyte the series endpoint was
// last asked for, so a test can assert the request as well as the rendered chart.
function mountHandlers(opts: {
  analytes?: LabAnalyte[];
  pending?: { attachment_id: number; note_id: number; note_slug: string; note_title: string }[];
  seriesFor?: (analyte: string) => LabSeries;
} = {}) {
  const analytes = opts.analytes ?? [GLUCOSE, SODIUM];
  const pending = opts.pending ?? [];
  const seriesFor = opts.seriesFor ?? ((a: string) => (a === "sodium" ? SODIUM_SERIES : GLUCOSE_SERIES));
  const seen = { analyte: null as string | null, count: 0 };
  server.use(
    http.get("*/api/medical/labs/pending", () => HttpResponse.json({ pending })),
    http.get("*/api/medical/labs/analytes", () => HttpResponse.json({ analytes })),
    http.get("*/api/medical/labs/series", ({ request }) => {
      const a = new URL(request.url).searchParams.get("analyte") || "";
      seen.analyte = a; seen.count++;
      return HttpResponse.json(seriesFor(a));
    }),
  );
  return seen;
}

beforeEach(() => { setAccessKey("test-key"); });
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) Analyte list loads & renders; first analyte auto-selects and charts
// ---------------------------------------------------------------------------- //
describe("LabsPage — list + selection", () => {
  it("loads the analyte list and renders a picker button per result", async () => {
    mountHandlers();
    renderWithProviders(<LabsPage />, { route: "/labs" });

    expect(await screen.findByRole("button", { name: /Glucose/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sodium/ })).toBeInTheDocument();
    // Each pick shows its last value + unit.
    expect(screen.getByText(/142 mg\/dL/)).toBeInTheDocument();
    expect(screen.getByText(/140 mmol\/L/)).toBeInTheDocument();
  });

  it("auto-selects the first analyte, requests its series, and renders its chart", async () => {
    const seen = mountHandlers();
    renderWithProviders(<LabsPage />, { route: "/labs" });

    // The first (abnormal-recent → top-sorted) analyte is glucose; its series is fetched
    // and the LabChart svg renders both points.
    const svg = await screen.findByRole("img", { name: /Glucose trend chart/i });
    expect(svg.querySelectorAll('circle[fill="transparent"]')).toHaveLength(2);
    await waitFor(() => expect(seen.analyte).toBe("glucose"));
  });

  it("selecting a different analyte fetches and charts that analyte's series", async () => {
    const seen = mountHandlers();
    const { user } = renderWithProviders(<LabsPage />, { route: "/labs" });

    await screen.findByRole("img", { name: /Glucose trend chart/i });
    await user.click(screen.getByRole("button", { name: /Sodium/ }));

    // The series endpoint is re-hit for sodium and the chart swaps to it.
    await waitFor(() => expect(seen.analyte).toBe("sodium"));
    expect(await screen.findByRole("img", { name: /Sodium trend chart/i })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) Time-window controls — preset chips re-window the chart's in-view set
// ---------------------------------------------------------------------------- //
describe("LabsPage — time window controls", () => {
  it("the '1y' preset narrows the window so older points drop out of view", async () => {
    // A wide series (2023→2026); the 1y preset clamps to [domain.to-1y, domain.to].
    mountHandlers({
      analytes: [GLUCOSE],
      seriesFor: () => series({
        analyte: "glucose", test_name: "Glucose",
        points: [pt({ t: "2023-01-01", v: 80 }), pt({ t: "2024-06-01", v: 90 }), pt({ t: "2026-05-01", v: 142 })],
        domain: { from: "2023-01-01", to: "2026-05-01" },
      }),
    });
    const { user } = renderWithProviders(<LabsPage />, { route: "/labs" });

    const svg = await screen.findByRole("img", { name: /Glucose trend chart/i });
    // Full domain on first load → all three points in view.
    await waitFor(() => expect(svg.querySelectorAll('circle[fill="transparent"]')).toHaveLength(3));

    await user.click(screen.getByRole("button", { name: "1y" }));
    // Only the 2026-05-01 point falls inside the last year → one glyph.
    await waitFor(() =>
      expect(
        screen.getByRole("img", { name: /Glucose trend chart/i }).querySelectorAll('circle[fill="transparent"]'),
      ).toHaveLength(1),
    );
  });
});

// ---------------------------------------------------------------------------- //
// (c) Navigation — the pending banner links to a note; point sources link out
// ---------------------------------------------------------------------------- //
describe("LabsPage — navigation links", () => {
  it("shows the import-review banner linking to the first pending note", async () => {
    mountHandlers({
      pending: [
        { attachment_id: 7, note_id: 7, note_slug: "labs-2026-01", note_title: "Quest labs" },
        { attachment_id: 8, note_id: 8, note_slug: "labs-2026-02", note_title: "More labs" },
      ],
    });
    renderWithProviders(<LabsPage />, { route: "/labs" });

    // Plural copy reflects the count; the Review link targets the first pending note.
    expect(await screen.findByText(/2 lab imports awaiting review\./)).toBeInTheDocument();
    const review = screen.getByRole("link", { name: /Review/ });
    expect(review).toHaveAttribute("href", "/note/labs-2026-01");
  });

  it("a picked point's source links to its note", async () => {
    mountHandlers({
      analytes: [GLUCOSE],
      seriesFor: () => series({
        analyte: "glucose", test_name: "Glucose",
        points: [
          pt({ t: "2025-02-01", v: 95, note_slug: "draw-feb" }),
          pt({ t: "2026-05-01", v: 142, status: "high", note_slug: "draw-may" }),
        ],
      }),
    });
    const { user } = renderWithProviders(<LabsPage />, { route: "/labs" });

    const svg = await screen.findByRole("img", { name: /Glucose trend chart/i });
    // Click the first point's hit-target; the detail panel renders a "View source →" link.
    const hit = svg.querySelector('circle[fill="transparent"]')!.parentElement as Element;
    await user.click(hit);
    const link = await screen.findByRole("link", { name: /View source/ });
    expect(link).toHaveAttribute("href", "/note/draw-feb");
  });
});

// ---------------------------------------------------------------------------- //
// (d) Share — the LabShareCreator modal opens and mints a link over MSW
// ---------------------------------------------------------------------------- //
describe("LabsPage — share results", () => {
  it("opens the share modal, picks a result, and mints a link (POST /api/shares/labs)", async () => {
    mountHandlers();
    let body: any = null;
    server.use(
      http.post("*/api/shares/labs", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ token: "tok", link_id: 1, url: "https://share.example/tok" });
      }),
    );
    const { user } = renderWithProviders(<LabsPage />, { route: "/labs" });

    await screen.findByRole("button", { name: /Glucose/ });
    await user.click(screen.getByRole("button", { name: /Share results/ }));

    // Tick a result in the modal, then create the link.
    const checks = await screen.findAllByRole("checkbox");
    await user.click(checks[0]);
    await user.click(screen.getByRole("button", { name: "Create link" }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(Array.isArray(body.analytes)).toBe(true);
    expect(body.analytes.length).toBe(1);
    // The minted link surfaces in the read-only URL field.
    expect((await screen.findByDisplayValue("https://share.example/tok"))).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (e) Empty / error states
// ---------------------------------------------------------------------------- //
describe("LabsPage — empty & error states", () => {
  it("renders the empty-state copy when no analytes are on file (no series fetch)", async () => {
    const seen = mountHandlers({ analytes: [] });
    renderWithProviders(<LabsPage />, { route: "/labs" });

    expect(await screen.findByText(/No lab results yet\./)).toBeInTheDocument();
    // With no analytes there's nothing to select → the series endpoint is never hit.
    expect(seen.count).toBe(0);
    expect(screen.queryByRole("img", { name: /trend chart/i })).not.toBeInTheDocument();
  });

  it("surfaces a load error when the analytes endpoint fails", async () => {
    server.use(
      http.get("*/api/medical/labs/pending", () => HttpResponse.json({ pending: [] })),
      http.get("*/api/medical/labs/analytes", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    renderWithProviders(<LabsPage />, { route: "/labs" });

    expect(await screen.findByText("Couldn't load labs.")).toBeInTheDocument();
  });

  it("restores the last-viewed analyte from localStorage and fetches its series", async () => {
    localStorage.setItem("jbrain.labs.sel", "sodium");
    const seen = mountHandlers();
    renderWithProviders(
      <Routes><Route path="/labs" element={<LabsPage />} /></Routes>,
      { route: "/labs" },
    );

    // The restored analyte (sodium) is charted instead of the top-sorted glucose.
    expect(await screen.findByRole("img", { name: /Sodium trend chart/i })).toBeInTheDocument();
    await waitFor(() => expect(seen.analyte).toBe("sodium"));
  });
});
