// Component-integration (Tier 2) tests for LabChart — the dependency-free SVG trend
// chart for ONE lab analyte over time. It renders a stepped reference band (ranges
// change over time), encounter shading, a line that breaks across censored points and
// long gaps, and points dual-encoded by SHAPE + COLOUR. The visible window is seeded
// from from/to props and driven by pan/zoom gestures; double-tap resets and reports up.
//
// Chart stubbing: NONE needed. LabChart emits its own <svg> (no recharts/canvas), and
// jsdom renders SVG elements fine — so we assert the DATA the chart draws (polyline
// point coords, glyph shapes per status, reference-band rects, axis tick labels, the
// in-view set, the selected-point tooltip text) rather than pixels.
//
// Network: LabChart itself is pure (series in via props, no fetch). The fetch → render
// flow is exercised through LabChartCard, which re-fetches the LIVE series via
// getLabSeries() and renders a <LabChart> — so those cases go through MSW (setup.ts
// runs onUnhandledRequest:"error", so every endpoint hit is stubbed via server.use).
//
// Determinism: querying is by role/text/DOM, and the visible window is fully determined
// by the series.domain / from / to we pass — no wall-clock dependence — so no fake
// timers (which would deadlock MSW) and no system-time pin are needed.
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import userEvent from "@testing-library/user-event";
import type { LabSeries, LabPoint, LabSegment, LabEncounter } from "../api";
import { setAccessKey } from "../api";

import LabChart from "./LabChart";
import LabChartCard from "./LabChartCard";

// ---- builders -------------------------------------------------------------- //
// A LabPoint with sane defaults; override per case. `t` is a date (the chart pads a
// short window around a single point), `v` is the numeric value used for the line/y.
function pt(o: Partial<LabPoint> & { t: string; v: number | null }): LabPoint {
  return {
    time: null,
    source: null,
    vtext: o.v == null ? "" : String(o.v),
    unit: "mg/dL",
    status: "normal",
    censored: false,
    ref_low: null,
    ref_high: null,
    ref_text: null,
    flag: null,
    note_id: null,
    note_slug: null,
    note_title: null,
    encounter_id: null,
    ...o,
  } as LabPoint;
}

function series(o: Partial<LabSeries> & { points: LabPoint[] }): LabSeries {
  const pts = o.points;
  const ts = pts.map((p) => p.t).sort();
  return {
    analyte: "glucose",
    test_name: "Glucose",
    unit: "mg/dL",
    segments: [] as LabSegment[],
    encounters: [] as LabEncounter[],
    other_units: [],
    value_range: null,
    domain: pts.length ? { from: ts[0], to: ts[ts.length - 1] } : null,
    ...o,
  } as LabSeries;
}

// The SVG plot area is 1000x360 with margins {l:52,r:14,t:12,b:34}; PW=934, PH=314.
const M_L = 52;
const PW = 1000 - 52 - 14;

afterEach(() => cleanup());

// Convenience: the chart's root <svg role="img"> for the default Glucose series.
function chart() {
  return screen.getByRole("img", { name: /Glucose trend chart/i });
}
// Numeric x of the first polyline's first point (the chart maps time → x within PW).
function firstLineXs(svg: HTMLElement): number[] {
  const line = svg.querySelector("polyline") as SVGPolylineElement | null;
  if (!line) return [];
  return (line.getAttribute("points") || "")
    .trim()
    .split(/\s+/)
    .map((pair) => Number(pair.split(",")[0]));
}

// ---------------------------------------------------------------------------- //
// (a) Series render — line, glyphs, axis labels for a multi-point analyte
// ---------------------------------------------------------------------------- //
describe("LabChart — series render", () => {
  it("draws a polyline through every in-window numeric point and labels the analyte", () => {
    renderWithProviders(
      <LabChart
        series={series({
          points: [
            pt({ t: "2026-01-10", v: 90 }),
            pt({ t: "2026-03-10", v: 110 }),
            pt({ t: "2026-05-10", v: 95 }),
          ],
        })}
      />,
    );
    const svg = chart();
    // One run (no censored/gap breaks) → a single polyline with three vertices.
    const lines = svg.querySelectorAll("polyline");
    expect(lines).toHaveLength(1);
    const xs = firstLineXs(svg);
    expect(xs).toHaveLength(3);
    // x is monotonically increasing in time and stays inside the plot area.
    expect(xs[0]).toBeLessThan(xs[1]);
    expect(xs[1]).toBeLessThan(xs[2]);
    expect(xs[0]).toBeGreaterThanOrEqual(M_L);
    expect(xs[2]).toBeLessThanOrEqual(M_L + PW + 0.5);
    // Three point hit-targets (transparent r=14 circles), one per in-view point.
    expect(svg.querySelectorAll('circle[fill="transparent"]')).toHaveLength(3);
    // Adaptive x ticks render month/day labels for this ~4-month span.
    expect(svg.querySelector("text")).not.toBeNull();
  });

  it("dual-encodes point status by SHAPE: high=triangle-up, low=triangle-down, abnormal=square", () => {
    renderWithProviders(
      <LabChart
        series={series({
          test_name: "Potassium",
          points: [
            pt({ t: "2026-01-10", v: 120, status: "high" }),
            pt({ t: "2026-02-10", v: 60, status: "low" }),
            pt({ t: "2026-03-10", v: 88, status: "abnormal" }),
            pt({ t: "2026-04-10", v: 95, status: "unknown" }),
          ],
        })}
      />,
    );
    const svg = screen.getByRole("img", { name: /Potassium trend chart/i });
    // high + low → two <path> triangles; abnormal → a filled <rect> with danger fill;
    // unknown → a hollow circle (fill var(--bg-elev), coloured stroke).
    const paths = svg.querySelectorAll("path");
    expect(paths.length).toBe(2);
    // The plot area itself is a faint var(--danger) backdrop (fill-opacity 0.06); the
    // ABNORMAL glyph is the solid var(--danger) rect (no fill-opacity), so exclude the
    // backdrop to count just the glyph square.
    const dangerRect = Array.from(svg.querySelectorAll("rect")).filter(
      (r) => r.getAttribute("fill") === "var(--danger)" && !r.getAttribute("fill-opacity"),
    );
    expect(dangerRect.length).toBe(1);
    const hollow = Array.from(svg.querySelectorAll("circle")).filter(
      (c) => c.getAttribute("fill") === "var(--bg-elev)",
    );
    expect(hollow.length).toBe(1);
  });
});

// ---------------------------------------------------------------------------- //
// (b) Reference-range bands — stepped segments draw as rects; encounters shade
// ---------------------------------------------------------------------------- //
describe("LabChart — reference bands & encounters", () => {
  it("renders one reference-range band rect per in-view segment (var(--ok))", () => {
    renderWithProviders(
      <LabChart
        series={series({
          points: [pt({ t: "2026-01-10", v: 95 }), pt({ t: "2026-06-10", v: 100 })],
          segments: [
            { from: "2026-01-01", to: "2026-03-31", low: 80, high: 110 },
            { from: "2026-04-01", to: "2026-06-30", low: 85, high: 115 },
          ],
        })}
      />,
    );
    const svg = chart();
    const bands = Array.from(svg.querySelectorAll("rect")).filter(
      (r) => r.getAttribute("fill") === "var(--ok)",
    );
    expect(bands).toHaveLength(2);
    // Both bands have positive width/height (they're inside the view, not collapsed).
    for (const b of bands) {
      expect(Number(b.getAttribute("width"))).toBeGreaterThan(0);
      expect(Number(b.getAttribute("height"))).toBeGreaterThan(0);
    }
  });

  it("shades encounters with an accent rect carrying the encounter label as a <title>", () => {
    renderWithProviders(
      <LabChart
        series={series({
          points: [pt({ t: "2026-02-01", v: 95 }), pt({ t: "2026-04-01", v: 100 })],
          encounters: [
            { id: 1, kind: "admission", label: "Hospital stay", from: "2026-02-10", to: "2026-02-20" },
          ],
        })}
      />,
    );
    const svg = chart();
    const enc = Array.from(svg.querySelectorAll("rect")).find(
      (r) => r.getAttribute("fill") === "var(--accent)",
    );
    expect(enc).toBeDefined();
    expect(enc!.querySelector("title")?.textContent).toBe("Hospital stay");
  });
});

// ---------------------------------------------------------------------------- //
// (c) Time window — from/to seeds the visible window and filters the in-view set
// ---------------------------------------------------------------------------- //
describe("LabChart — time window", () => {
  const wide = series({
    points: [
      pt({ t: "2024-01-01", v: 80 }),
      pt({ t: "2025-01-01", v: 90 }),
      pt({ t: "2026-01-01", v: 100 }),
    ],
    domain: { from: "2024-01-01", to: "2026-01-01" },
  });

  it("with no window shows the full domain → all points are in view", () => {
    renderWithProviders(<LabChart series={wide} />);
    const svg = chart();
    // All three points have a hit-target circle (only in-view points render glyphs).
    expect(svg.querySelectorAll('circle[fill="transparent"]')).toHaveLength(3);
  });

  it("a narrow from/to window filters the rendered points to that span", () => {
    renderWithProviders(<LabChart series={wide} from="2025-09-01" to="2026-03-01" />);
    const svg = chart();
    // Only the 2026-01-01 point falls inside the requested window → one glyph.
    expect(svg.querySelectorAll('circle[fill="transparent"]')).toHaveLength(1);
  });

  it("re-rendering with a new from/to prop refilters the in-view points", () => {
    const { rerender } = renderWithProviders(
      <LabChart series={wide} from="2025-09-01" to="2026-03-01" />,
    );
    expect(chart().querySelectorAll('circle[fill="transparent"]')).toHaveLength(1);

    // Widen the window to cover 2024-2026: the effect resets the window to the props.
    rerender(<LabChart series={wide} from="2023-06-01" to="2026-06-01" />);
    expect(chart().querySelectorAll('circle[fill="transparent"]')).toHaveLength(3);
  });

  it("double-click resets to the full domain and reports the new window upward", async () => {
    const onViewChange = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <LabChart
        series={wide}
        from="2025-09-01"
        to="2026-03-01"
        onViewChange={onViewChange}
      />,
    );
    await user.dblClick(chart());
    // Reset reports the full data extent (the domain) back to the parent.
    await waitFor(() => expect(onViewChange).toHaveBeenCalledWith("2024-01-01", "2026-01-01"));
    // And all points are now in view again.
    expect(chart().querySelectorAll('circle[fill="transparent"]')).toHaveLength(3);
  });
});

// ---------------------------------------------------------------------------- //
// (d) Empty / single-point / censored-break edge states
// ---------------------------------------------------------------------------- //
describe("LabChart — edge states", () => {
  it("an empty series still renders the chart frame but no line or points", () => {
    renderWithProviders(<LabChart series={series({ points: [] })} />);
    const svg = chart();
    expect(svg.querySelector("polyline")).toBeNull();
    expect(svg.querySelectorAll('circle[fill="transparent"]')).toHaveLength(0);
    // Axis ticks (the y gridline labels) still render so the empty frame is meaningful.
    expect(svg.querySelectorAll("text").length).toBeGreaterThan(0);
  });

  it("a single point pads the domain and renders one glyph with no connecting line", () => {
    renderWithProviders(
      <LabChart series={series({ points: [pt({ t: "2026-03-15", v: 99 })] })} />,
    );
    const svg = chart();
    // A lone point still forms a one-vertex run, so a <polyline> is emitted — but with a
    // SINGLE coordinate it draws no connecting segment. The glyph (its hit-target) is the
    // visible mark.
    const lines = svg.querySelectorAll("polyline");
    expect(lines).toHaveLength(1);
    const verts = (lines[0].getAttribute("points") || "").trim().split(/\s+/).filter(Boolean);
    expect(verts).toHaveLength(1);
    expect(svg.querySelectorAll('circle[fill="transparent"]')).toHaveLength(1);
  });

  it("censored points break the line into separate runs and render a ⌄ marker", () => {
    renderWithProviders(
      <LabChart
        series={series({
          points: [
            pt({ t: "2026-01-10", v: 90 }),
            pt({ t: "2026-02-10", v: 95 }),
            // A censored point (e.g. "<5") breaks the run; it has no numeric v.
            pt({ t: "2026-03-10", v: null, vtext: "<5", censored: true }),
            pt({ t: "2026-04-10", v: 100 }),
            pt({ t: "2026-05-10", v: 105 }),
          ],
        })}
      />,
    );
    const svg = chart();
    // Two numeric runs (before and after the censored gap) → two polylines.
    expect(svg.querySelectorAll("polyline")).toHaveLength(2);
    // The censored marker glyph renders as a "⌄" text node.
    const censoredMark = Array.from(svg.querySelectorAll("text")).find(
      (t) => t.textContent === "⌄",
    );
    expect(censoredMark).toBeDefined();
  });
});

// ---------------------------------------------------------------------------- //
// (e) Unit / value rendering — tapping a point opens its detail tooltip
// ---------------------------------------------------------------------------- //
describe("LabChart — point detail (value + unit)", () => {
  it("clicking a point shows its value, unit, status, date and ref text; onPick fires", async () => {
    const onPick = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <LabChart
        series={series({
          points: [
            pt({
              t: "2026-03-15",
              v: 142,
              vtext: "142",
              unit: "mg/dL",
              status: "high",
              ref_text: "70-99",
            }),
            pt({ t: "2026-04-15", v: 96, status: "normal" }),
          ],
        })}
        onPick={onPick}
      />,
    );
    const svg = chart();
    // Click the first point's hit-target group (the transparent r=14 circle's parent).
    const hit = svg.querySelector('circle[fill="transparent"]')!.parentElement as Element;
    await user.click(hit);

    expect(onPick).toHaveBeenCalledTimes(1);
    expect(onPick.mock.calls[0][0]).toMatchObject({ vtext: "142", unit: "mg/dL", status: "high" });
    // The tooltip renders "142 mg/dL · high" and the date + ref range.
    expect(await screen.findByText(/142 mg\/dL · high/)).toBeInTheDocument();
    expect(screen.getByText(/2026-03-15.*\(ref 70-99\)/)).toBeInTheDocument();
  });

  it("clicking the chart background clears the selection (onPick null)", async () => {
    const onPick = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <LabChart
        series={series({ points: [pt({ t: "2026-03-15", v: 99 })] })}
        onPick={onPick}
      />,
    );
    await user.click(chart());
    expect(onPick).toHaveBeenLastCalledWith(null);
  });
});

// ---------------------------------------------------------------------------- //
// (f) Fetch → render flow via LabChartCard (the live re-fetch wrapper) over MSW
// ---------------------------------------------------------------------------- //
describe("LabChart — fetch flow (LabChartCard over MSW)", () => {
  const RESULT: LabSeries = series({
    analyte: "glucose",
    test_name: "Glucose",
    unit: "mg/dL",
    points: [pt({ t: "2026-02-01", v: 95 }), pt({ t: "2026-05-01", v: 102 })],
  });

  it("fetches the series for the spec analyte then renders the LabChart with its points", async () => {
    setAccessKey("test-key");
    let asked: { analyte: string | null } = { analyte: null };
    server.use(
      http.get("*/api/medical/labs/series", ({ request }) => {
        asked = { analyte: new URL(request.url).searchParams.get("analyte") };
        return HttpResponse.json(RESULT);
      }),
    );
    renderWithProviders(<LabChartCard spec={{ analyte: "glucose", title: "Glucose" }} />);

    // Loading placeholder first…
    expect(screen.getByText(/Charting Glucose…/i)).toBeInTheDocument();
    // …then the fetched chart (the LabChart svg renders the two points).
    const svg = await screen.findByRole("img", { name: /Glucose trend chart/i });
    expect(svg.querySelectorAll('circle[fill="transparent"]')).toHaveLength(2);
    // The card requested the right analyte.
    expect(asked.analyte).toBe("glucose");
    expect(screen.getByText("Glucose")).toBeInTheDocument();
  });

  it("renders an empty-state message when the fetched series has no points", async () => {
    setAccessKey("test-key");
    server.use(
      http.get("*/api/medical/labs/series", () =>
        HttpResponse.json(series({ test_name: "Glucose", points: [] })),
      ),
    );
    renderWithProviders(<LabChartCard spec={{ analyte: "glucose" }} />);
    expect(await screen.findByText(/No results on file for Glucose/i)).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /trend chart/i })).not.toBeInTheDocument();
  });

  it("passes the spec window (from/to) into the chart so it filters to that span", async () => {
    setAccessKey("test-key");
    server.use(
      http.get("*/api/medical/labs/series", () =>
        HttpResponse.json(
          series({
            test_name: "Glucose",
            points: [
              pt({ t: "2024-01-01", v: 80 }),
              pt({ t: "2026-01-01", v: 100 }),
            ],
            domain: { from: "2024-01-01", to: "2026-01-01" },
          }),
        ),
      ),
    );
    renderWithProviders(
      <LabChartCard
        spec={{ analyte: "glucose", title: "Glucose", from: "2025-09-01", to: "2026-03-01" }}
      />,
    );
    const svg = await screen.findByRole("img", { name: /Glucose trend chart/i });
    // Only the 2026 point is inside the spec window.
    expect(svg.querySelectorAll('circle[fill="transparent"]')).toHaveLength(1);
  });
});
