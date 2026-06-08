// Component-integration (Tier 2) tests for GraphPage — the knowledge-graph view.
// react-force-graph-2d renders to canvas/WebGL, which jsdom can't drive, so we stub
// it with a plain <div> that surfaces the graph DATA (node/link counts) as data-*
// attributes and stashes the live props (graphData + click handlers) on a module
// shared ref. That lets the tests assert the DATA FLOW into the graph and invoke the
// click handlers directly, instead of trying to render the force simulation.
//
// MSW is the only network mock (setup.ts runs onUnhandledRequest:"error"), so every
// endpoint the page hits is stubbed — here just GET /api/graph. The page calls
// useNavigate(); we capture navigations via a stubbed react-router-dom hook.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderWithProviders, screen, waitFor } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";

// GraphPage doesn't import useAuth, but the project standard stubs ../App so a page
// test never drags in the auth/router App tree. Harmless + matches NotePage.test.tsx.
vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

// Capture the props the page passes to <ForceGraph2D> on each render. The stub mirrors
// node/link counts onto data-* attrs (queryable) and keeps the latest props (incl. the
// onNodeClick / onBackgroundClick handlers) so tests can drive interaction directly.
let fgProps: any = null;
vi.mock("react-force-graph-2d", () => ({
  default: (props: any) => {
    fgProps = props;
    return (
      <div
        data-testid="force-graph"
        data-nodes={props.graphData?.nodes?.length ?? 0}
        data-links={props.graphData?.links?.length ?? 0}
      />
    );
  },
}));

// d3-force-3d's forceCollide is only used inside an effect that pokes fgRef.d3Force();
// our stub ref has no such method (the effect early-returns on a bare ref object), so
// the real module is fine — but stub it to avoid importing the d3 stack into jsdom.
vi.mock("d3-force-3d", () => ({ forceCollide: () => ({}) }));

// Capture navigate() calls (second click on a node → /note/:slug; "Open note" button).
const navigate = vi.fn();
vi.mock("react-router-dom", async (orig) => {
  const actual = await orig<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigate };
});

import GraphPage from "./GraphPage";

// A small graph: 2 KB articles, 1 entry, 1 list, with links among them. `val` drives
// node radius; `kind` drives both colour and the kind filter.
const NODES = [
  { id: 1, title: "kb/People/Alice", slug: "kb-people-alice", kind: "kb", val: 4 },
  { id: 2, title: "kb/People/Bob", slug: "kb-people-bob", kind: "kb", val: 3 },
  { id: 3, title: "notes/Lunch with Alice", slug: "notes-lunch", kind: "entry", val: 2 },
  { id: 4, title: "lists/Groceries", slug: "lists-groceries", kind: "list", val: 1 },
];
const LINKS = [
  { source: 1, target: 3 }, // Alice (kb) — Lunch (entry)
  { source: 2, target: 3 }, // Bob (kb) — Lunch (entry)
  { source: 3, target: 4 }, // Lunch (entry) — Groceries (list)
];

function graphHandler(body: { nodes: unknown[]; links: unknown[] } = { nodes: NODES, links: LINKS }) {
  server.use(http.get("*/api/graph", () => HttpResponse.json(body)));
}

beforeEach(() => {
  fgProps = null;
  navigate.mockReset();
  // GraphPage measures its wrapper via getComputedStyle for the focus-ring colour and
  // reads clientWidth/Height (0 in jsdom — fine, the stub ignores size). No matchMedia
  // needed. Nothing else to seed.
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("GraphPage", () => {
  it("fetches graph data on mount and passes every node/link to the force-graph", async () => {
    graphHandler();
    renderWithProviders(<GraphPage />);

    const fg = await screen.findByTestId("force-graph");
    // All 4 nodes and all 3 links flow into the graph under the default "All" filter.
    await waitFor(() => expect(fg.getAttribute("data-nodes")).toBe("4"));
    expect(fg.getAttribute("data-links")).toBe("3");
    // The hint copy renders when there are nodes and nothing is focused.
    expect(screen.getByText(/Tap a node to focus/)).toBeInTheDocument();
  });

  it("filtering by kind narrows the data handed to the force-graph", async () => {
    graphHandler();
    const { user } = renderWithProviders(<GraphPage />);

    const fg = await screen.findByTestId("force-graph");
    await waitFor(() => expect(fg.getAttribute("data-nodes")).toBe("4"));

    // Click the "Entries" segment → only the single entry node survives the filter,
    // and with one node there are no surviving edges.
    await user.click(screen.getByRole("button", { name: "Entries" }));
    await waitFor(() => expect(fg.getAttribute("data-nodes")).toBe("1"));
    expect(fg.getAttribute("data-links")).toBe("0");

    // Click "KB" → both KB articles remain. They link only via the shared (now hidden)
    // entry, so the page's path-projection at the default 2-hop KB budget reconnects
    // them: 2 nodes, 1 projected edge.
    await user.click(screen.getByRole("button", { name: "KB" }));
    await waitFor(() => expect(fg.getAttribute("data-nodes")).toBe("2"));
    expect(fg.getAttribute("data-links")).toBe("1");
  });

  it("changing the hop budget refilters the projected edges", async () => {
    graphHandler();
    const { user } = renderWithProviders(<GraphPage />);

    const fg = await screen.findByTestId("force-graph");
    await waitFor(() => expect(fg.getAttribute("data-nodes")).toBe("4"));

    // Switch to KB (default 2 hops → the two KB nodes are joined through the entry).
    await user.click(screen.getByRole("button", { name: "KB" }));
    await waitFor(() => expect(fg.getAttribute("data-links")).toBe("1"));

    // Drop to 1 hop: the KB↔KB path runs through a hidden entry (2 hops), so within a
    // single hop neither KB node reaches the other → the projected edge disappears.
    await user.selectOptions(screen.getByRole("combobox"), "1");
    await waitFor(() => expect(fg.getAttribute("data-links")).toBe("0"));
    expect(fg.getAttribute("data-nodes")).toBe("2");
  });

  it("first node click focuses (no navigation), second click opens the note", async () => {
    graphHandler();
    renderWithProviders(<GraphPage />);

    await screen.findByTestId("force-graph");
    await waitFor(() => expect(fgProps?.graphData?.nodes?.length).toBe(4));

    const alice = NODES[0];
    // First click on a node focuses it: a focus banner appears, no navigation yet.
    act(() => { fgProps.onNodeClick({ id: alice.id, slug: alice.slug }); });
    expect(await screen.findByText("kb/People/Alice")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Open note/ })).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();

    // Second click on the SAME node navigates to its note.
    act(() => { fgProps.onNodeClick({ id: alice.id, slug: alice.slug }); });
    expect(navigate).toHaveBeenCalledWith("/note/kb-people-alice");
  });

  it("the focus banner's Open note button navigates and Clear unfocuses", async () => {
    graphHandler();
    const { user } = renderWithProviders(<GraphPage />);

    await screen.findByTestId("force-graph");
    await waitFor(() => expect(fgProps?.graphData?.nodes?.length).toBe(4));

    // Focus the entry node, which has the richest neighbourhood.
    act(() => { fgProps.onNodeClick({ id: 3, slug: "notes-lunch" }); });
    expect(await screen.findByText("notes/Lunch with Alice")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Open note/ }));
    expect(navigate).toHaveBeenCalledWith("/note/notes-lunch");

    // Clear the focus → banner and its Open-note button go away.
    await user.click(screen.getByRole("button", { name: /Clear/ }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Open note/ })).not.toBeInTheDocument(),
    );
  });

  it("a background click clears the current focus", async () => {
    graphHandler();
    renderWithProviders(<GraphPage />);

    await screen.findByTestId("force-graph");
    await waitFor(() => expect(fgProps?.graphData?.nodes?.length).toBe(4));

    act(() => { fgProps.onNodeClick({ id: 1, slug: "kb-people-alice" }); });
    expect(await screen.findByText("kb/People/Alice")).toBeInTheDocument();

    act(() => { fgProps.onBackgroundClick(); });
    await waitFor(() =>
      expect(screen.queryByText("kb/People/Alice")).not.toBeInTheDocument(),
    );
  });

  it("renders the empty state when the graph has no notes", async () => {
    graphHandler({ nodes: [], links: [] });
    renderWithProviders(<GraphPage />);

    expect(await screen.findByText("No notes to graph yet.")).toBeInTheDocument();
    const fg = screen.getByTestId("force-graph");
    expect(fg.getAttribute("data-nodes")).toBe("0");
    expect(fg.getAttribute("data-links")).toBe("0");
    // The "tap a node" hint is suppressed when there's nothing to graph.
    expect(screen.queryByText(/Tap a node to focus/)).not.toBeInTheDocument();
  });

  it("swallows a fetch error and shows the empty state", async () => {
    server.use(http.get("*/api/graph", () => HttpResponse.json({ detail: "boom" }, { status: 500 })));
    renderWithProviders(<GraphPage />);

    // The mount effect's .catch(() => {}) leaves data at its empty initial value, so
    // the page renders its empty-graph copy rather than crashing.
    expect(await screen.findByText("No notes to graph yet.")).toBeInTheDocument();
    expect(screen.getByTestId("force-graph").getAttribute("data-nodes")).toBe("0");
  });
});
