// Component-integration (Tier 2) tests for ShareChart — one scoped trend chart for a
// SHARE recipient. It fetches its series for THIS token through the token-scoped public
// endpoint (getShareLabSeries for variant 'labs', getResearchLabSeries for 'research'),
// then renders the shared LabChart SVG. Loading / error / empty states degrade quietly.
//
// Network is the only thing to mock; setup.ts runs onUnhandledRequest:"error", so each
// flow stubs exactly the endpoint it touches via server.use(...). The child LabChart is
// pure (series via props, its own <svg> — no fetch). No router needed (no Link/navigate),
// so plain render suffices.
import { afterEach, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import type { LabSeries, LabPoint } from "../api";

import ShareChart from "./ShareChart";

const LABS_PATH = "*/api/share/:token/labs/series";
const RESEARCH_PATH = "*/api/share/:token/research/labs/series";

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

function series(o: Partial<LabSeries> & { points: LabPoint[] }): LabSeries {
  const ts = o.points.map((p) => p.t).sort();
  return {
    analyte: "glucose", test_name: "Glucose", unit: "mg/dL",
    segments: [], encounters: [], other_units: [], value_range: null,
    domain: ts.length ? { from: ts[0], to: ts[ts.length - 1] } : null,
    ...o,
  } as LabSeries;
}

const GLUCOSE = series({
  test_name: "Glucose", unit: "mg/dL",
  points: [pt({ t: "2026-01-10", v: 95 }), pt({ t: "2026-05-10", v: 102 })],
});

afterEach(cleanup);

describe("ShareChart", () => {
  it("shows a loading placeholder, then fetches the labs series and renders the chart", async () => {
    let asked: { analyte: string | null } = { analyte: null };
    server.use(
      http.get(LABS_PATH, ({ request }) => {
        asked = { analyte: new URL(request.url).searchParams.get("analyte") };
        return HttpResponse.json(GLUCOSE);
      }),
    );
    render(<ShareChart token="tok123" analyte="glucose" />);

    // Loading state first (titled by analyte since no title prop).
    expect(screen.getByText(/Loading glucose/i)).toBeInTheDocument();
    // Then the fetched chart: header shows test_name + unit, and the SVG draws 2 points.
    const svg = await screen.findByRole("img", { name: /Glucose trend chart/i });
    expect(svg.querySelectorAll('circle[fill="transparent"]')).toHaveLength(2);
    expect(screen.getByText("Glucose")).toBeInTheDocument();
    expect(screen.getByText(/\(mg\/dL\)/)).toBeInTheDocument();
    expect(asked.analyte).toBe("glucose");
  });

  it("uses the research endpoint when variant='research'", async () => {
    let hitResearch = false;
    server.use(
      http.get(RESEARCH_PATH, () => { hitResearch = true; return HttpResponse.json(GLUCOSE); }),
    );
    render(<ShareChart token="tok123" analyte="glucose" variant="research" />);
    await screen.findByRole("img", { name: /Glucose trend chart/i });
    expect(hitResearch).toBe(true);
  });

  it("renders a quiet error message when the fetch fails", async () => {
    server.use(
      http.get(LABS_PATH, () => HttpResponse.json({ detail: "nope" }, { status: 404 })),
    );
    render(<ShareChart token="tok123" analyte="glucose" title="Glucose level" />);
    expect(await screen.findByText(/Couldn’t load this result\./i)).toBeInTheDocument();
    // The title prop is used for the error header.
    expect(screen.getByText("Glucose level")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /trend chart/i })).not.toBeInTheDocument();
  });

  it("renders nothing when the fetched series has no points", async () => {
    server.use(
      http.get(LABS_PATH, () => HttpResponse.json(series({ points: [] }))),
    );
    const { container } = render(<ShareChart token="tok123" analyte="glucose" />);
    // After the fetch resolves to an empty series the component returns null.
    await waitFor(() => expect(screen.queryByText(/Loading/i)).not.toBeInTheDocument());
    expect(container).toBeEmptyDOMElement();
  });
});
