// Component-integration (Tier 2) tests for LabShareView — the RECIPIENT view of a
// data-only lab-share link (kind='labs'). The owner picked a FIXED, allow-listed set
// of analytes; the recipient enters a name, then views each result's windowed trend
// the same way the owner's Labs page does (pick a result → one chart updates).
//
// LabShareView talks ONLY to the cookie-auth public lab-share endpoints (see api.ts):
//   POST /api/share/:token/labs/start         → labsStart()         (the analyte list)
//   GET  /api/share/:token/labs/series?analyte=… → getShareLabSeries() (one scoped series)
// There is NO recipient AI Q&A in THIS component (the scoped chat lives in the assisted/
// research links via labsTurn — LabShareView never calls it; see the note in (e)). Every
// series comes ONLY through the token-scoped, allow-list-rechecked endpoint, never the
// owner /api/medical/* path, so scope enforcement = the picker shows exactly the
// allow-listed analytes and the chart fetch hits the share-scoped series URL.
//
// MSW is the only network mock and setup.ts runs onUnhandledRequest:"error", so every
// endpoint each flow touches is stubbed via server.use(...). The child LabChart is pure
// (series via props, its own <svg> — no fetch), so no chart stubbing / empty-points
// handler is needed here. The component takes token/brainName/intro/consent as PROPS
// (not route params), so no router param wiring and no vi.mock("../App") are required.
import { afterEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, within, cleanup } from "../test/render";
import { server } from "../test/server";
import type { LabSeries, LabPoint } from "../api";

import LabShareView from "./LabShareView";

const START_PATH = "*/api/share/:token/labs/start";
const SERIES_PATH = "*/api/share/:token/labs/series";

// ---- builders -------------------------------------------------------------- //
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

// A LabSeries with >=2 non-censored points + a domain so the chart actually draws
// (LabShareView gates the chart on points.filter(!censored).length >= 2).
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
  analyte: "glucose", test_name: "Glucose", unit: "mg/dL",
  points: [pt({ t: "2026-01-10", v: 95 }), pt({ t: "2026-05-10", v: 102 })],
});
const HDL = series({
  analyte: "hdl", test_name: "HDL Cholesterol", unit: "mg/dL",
  points: [pt({ t: "2026-02-01", v: 55 }), pt({ t: "2026-04-01", v: 60 })],
});

// The two allow-listed analytes the owner shared (the scope). test_name drives the
// picker label; `analyte` is the key the series endpoint is queried by.
const ANALYTES = [
  { analyte: "glucose", test_name: "Glucose", unit: "mg/dL" },
  { analyte: "hdl", test_name: "HDL Cholesterol", unit: "mg/dL" },
];

// Return the right scoped series for whichever analyte the view asks for.
function seriesByAnalyte(url: string): LabSeries {
  const a = new URL(url).searchParams.get("analyte");
  return a === "hdl" ? HDL : GLUCOSE;
}

function render(over: { intro?: string; consent?: string } = {}) {
  return renderWithProviders(
    <LabShareView token="TOKEN" brainName="Test Brain" {...over} />,
  );
}

// Walk through the consent gate: type a name and click "View results".
async function enterAndView(user: ReturnType<typeof renderWithProviders>["user"], name = "Pat") {
  await user.type(screen.getByPlaceholderText("Your name *"), name);
  await user.click(screen.getByRole("button", { name: "View results" }));
}

afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) Consent gate — intro/consent copy, name required, and labsStart on submit.
// ---------------------------------------------------------------------------- //
describe("LabShareView — consent gate", () => {
  it("renders the brand, intro and consent copy with a disclaimer banner", () => {
    render({ intro: "A few of my recent labs.", consent: "By viewing you agree to keep these private." });
    expect(screen.getByText("Test Brain’s shared lab results")).toBeInTheDocument();
    expect(screen.getByText("A few of my recent labs.")).toBeInTheDocument();
    expect(screen.getByText("By viewing you agree to keep these private.")).toBeInTheDocument();
    expect(screen.getByText(/Not a diagnosis/)).toBeInTheDocument();
    expect(screen.getByText("Shared · Lab results")).toBeInTheDocument();
  });

  it("an empty name alerts and does NOT call labsStart", async () => {
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    let started = false;
    server.use(http.post(START_PATH, () => { started = true; return HttpResponse.json({ analytes: ANALYTES }); }));
    const { user } = render();

    await user.click(screen.getByRole("button", { name: "View results" }));

    expect(alertSpy).toHaveBeenCalledWith("Please enter your name.");
    expect(started).toBe(false);
    alertSpy.mockRestore();
  });

  it("posts labsStart with the token + trimmed name and persists the name locally", async () => {
    let hitUrl = "";
    let body: any = null;
    server.use(http.post(START_PATH, async ({ request }) => {
      hitUrl = request.url;
      body = await request.json();
      return HttpResponse.json({ analytes: ANALYTES });
    }), http.get(SERIES_PATH, ({ request }) => HttpResponse.json(seriesByAnalyte(request.url))));
    const { user } = render();

    await enterAndView(user, "  Pat  ");

    await waitFor(() => expect(body).toEqual({ name: "Pat" }));
    expect(hitUrl).toContain("/api/share/TOKEN/labs/start");
    expect(localStorage.getItem("jbrain_share_name")).toBe("Pat");
  });

  it("surfaces a labsStart error and stays on the consent gate", async () => {
    server.use(http.post(START_PATH, () =>
      HttpResponse.json({ detail: "This link has expired." }, { status: 404 })));
    const { user } = render();

    await enterAndView(user);

    expect(await screen.findByText("This link has expired.")).toBeInTheDocument();
    // Still on the consent gate — no analyte picker rendered.
    expect(screen.getByRole("button", { name: "View results" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) Scoped analytes load + render — only the allow-listed results appear.
// ---------------------------------------------------------------------------- //
describe("LabShareView — scoped analyte list", () => {
  it("after start, renders ONLY the allow-listed analytes and auto-selects the first", async () => {
    server.use(
      http.post(START_PATH, () => HttpResponse.json({ analytes: ANALYTES })),
      http.get(SERIES_PATH, ({ request }) => HttpResponse.json(seriesByAnalyte(request.url))),
    );
    const { user } = render();

    await enterAndView(user);

    // Both shared results show in the picker…
    expect(await screen.findByRole("button", { name: /Glucose/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /HDL Cholesterol/ })).toBeInTheDocument();
    // …and nothing outside the scope (e.g. a result the owner did NOT share) appears.
    expect(screen.queryByRole("button", { name: /Sodium/ })).not.toBeInTheDocument();
    // The first analyte is auto-selected → its chart renders.
    expect(await screen.findByRole("img", { name: /Glucose trend chart/i })).toBeInTheDocument();
  });

  it("fetches the first analyte's series through the token-scoped share endpoint", async () => {
    let seriesUrl = "";
    server.use(
      http.post(START_PATH, () => HttpResponse.json({ analytes: ANALYTES })),
      http.get(SERIES_PATH, ({ request }) => {
        seriesUrl = request.url;
        return HttpResponse.json(seriesByAnalyte(request.url));
      }),
    );
    const { user } = render();

    await enterAndView(user);
    await screen.findByRole("img", { name: /Glucose trend chart/i });

    // Scope enforcement: the series comes via the share-scoped path, NOT /api/medical/*,
    // and is keyed by the selected (allow-listed) analyte.
    expect(seriesUrl).toContain("/api/share/TOKEN/labs/series");
    expect(seriesUrl).toContain("analyte=glucose");
    expect(seriesUrl).not.toContain("/api/medical/");
  });

  it("shows the empty state when the owner shared no analytes", async () => {
    server.use(http.post(START_PATH, () => HttpResponse.json({ analytes: [] })));
    const { user } = render();

    await enterAndView(user);

    expect(await screen.findByText("No results to show.")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /trend chart/i })).not.toBeInTheDocument();
  });

  it("filters the picker by the search box (multiple analytes only)", async () => {
    server.use(
      http.post(START_PATH, () => HttpResponse.json({ analytes: ANALYTES })),
      http.get(SERIES_PATH, ({ request }) => HttpResponse.json(seriesByAnalyte(request.url))),
    );
    const { user } = render();

    await enterAndView(user);
    await screen.findByRole("button", { name: /Glucose/ });

    await user.type(screen.getByPlaceholderText("Find a result…"), "HDL");
    expect(screen.getByRole("button", { name: /HDL Cholesterol/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Glucose/ })).not.toBeInTheDocument();

    await user.clear(screen.getByPlaceholderText("Find a result…"));
    await user.type(screen.getByPlaceholderText("Find a result…"), "zzz");
    expect(screen.getByText("No results match.")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (c) Selecting an analyte swaps the chart and re-fetches its scoped series.
// ---------------------------------------------------------------------------- //
describe("LabShareView — selecting a result", () => {
  it("clicking another analyte fetches + renders that result's chart", async () => {
    const asked: string[] = [];
    server.use(
      http.post(START_PATH, () => HttpResponse.json({ analytes: ANALYTES })),
      http.get(SERIES_PATH, ({ request }) => {
        asked.push(new URL(request.url).searchParams.get("analyte") || "");
        return HttpResponse.json(seriesByAnalyte(request.url));
      }),
    );
    const { user } = render();

    await enterAndView(user);
    await screen.findByRole("img", { name: /Glucose trend chart/i });

    await user.click(screen.getByRole("button", { name: /HDL Cholesterol/ }));

    // The chart swaps to HDL and the HDL series was fetched by its scoped key.
    expect(await screen.findByRole("img", { name: /HDL Cholesterol trend chart/i })).toBeInTheDocument();
    await waitFor(() => expect(asked).toContain("hdl"));
  });

  it("a series load failure shows the per-result error without leaving the view", async () => {
    server.use(
      http.post(START_PATH, () => HttpResponse.json({ analytes: ANALYTES })),
      http.get(SERIES_PATH, () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    const { user } = render();

    await enterAndView(user);

    expect(await screen.findByText("Couldn’t load that result.")).toBeInTheDocument();
    // Still in the viewing phase: the picker is present.
    expect(screen.getByRole("button", { name: /Glucose/ })).toBeInTheDocument();
  });

  it("a result with fewer than two values shows the single-draw note, not a chart", async () => {
    const onePoint = series({
      analyte: "glucose", test_name: "Glucose",
      points: [pt({ t: "2026-03-15", v: 99 })],
    });
    server.use(
      http.post(START_PATH, () => HttpResponse.json({ analytes: [ANALYTES[0]] })),
      http.get(SERIES_PATH, () => HttpResponse.json(onePoint)),
    );
    const { user } = render();

    await enterAndView(user);

    expect(await screen.findByText(/Only one result on 2026-03-15/)).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /trend chart/i })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (d) The scoped data-table fallback (identity-stripped) renders the same values.
// ---------------------------------------------------------------------------- //
describe("LabShareView — table fallback", () => {
  it("opening 'Show as table' lists each value with NO source/identity column", async () => {
    server.use(
      http.post(START_PATH, () => HttpResponse.json({ analytes: [ANALYTES[0]] })),
      http.get(SERIES_PATH, () => HttpResponse.json(GLUCOSE)),
    );
    const { user } = render();

    await enterAndView(user);
    await screen.findByRole("img", { name: /Glucose trend chart/i });

    await user.click(screen.getByText("Show as table"));

    const table = await screen.findByRole("table");
    // Columns are date/value/range/status only — no "source" (identity-stripped scope).
    const headers = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Date", "Value", "Range", "Status"]);
    expect(within(table).getByText("2026-01-10")).toBeInTheDocument();
    expect(within(table).getByText("95 mg/dL")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (e) No recipient AI Q&A in THIS component — scope verification.
//     The data-only kind has no chatbot (the scoped AI lives in assisted/research
//     links, via labsTurn, which LabShareView never calls). Assert there is no
//     question box and that the ONLY network the view drives is start + series.
// ---------------------------------------------------------------------------- //
describe("LabShareView — no chatbot (data-only scope)", () => {
  it("renders no Q&A/ask affordance and never POSTs labs/turn", async () => {
    let turned = false;
    server.use(
      http.post(START_PATH, () => HttpResponse.json({ analytes: ANALYTES })),
      http.get(SERIES_PATH, ({ request }) => HttpResponse.json(seriesByAnalyte(request.url))),
      // If the view ever called labsTurn this would flip; with onUnhandledRequest:"error"
      // an unexpected POST would otherwise fail the test loudly.
      http.post("*/api/share/:token/labs/turn", () => { turned = true; return HttpResponse.json({}); }),
    );
    const { user } = render();

    await enterAndView(user);
    await screen.findByRole("img", { name: /Glucose trend chart/i });

    // No ask/send affordance for an AI question over the labs.
    expect(screen.queryByRole("button", { name: /ask|send/i })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/ask/i)).not.toBeInTheDocument();
    expect(turned).toBe(false);
  });
});
