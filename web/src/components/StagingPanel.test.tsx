// Component-integration (Tier 2) tests for StagingPanel.tsx — the staging-area UI
// where the architect's proposed note changes wait for the owner to confirm. The
// panel fetches GET /api/staging on mount (and whenever `tick` changes), renders one
// card per staged action (type tag, title, diff/preview, per-action Apply/Reject and
// an Apply-all), and reflects success by re-loading + calling onChange().
//
// Network is MSW only (setup.ts runs onUnhandledRequest: "error"), so every endpoint
// the panel touches per flow is stubbed via server.use(...). The default status/auth
// handlers cover the rest. Each mutating endpoint records its request so routing +
// body can be asserted. jsdom has no native alert(), so it's stubbed for the error path.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, within, cleanup } from "../test/render";
import { server } from "../test/server";
import type { Preview } from "./ApprovalView";

import StagingPanel from "./StagingPanel";

// A staged action as GET /api/staging returns it. `preview` follows ApprovalView's
// uniform shape (text/fields/tags), with before==null => create, after==null => delete.
interface StagedAction {
  id: number;
  type: string;
  payload: Record<string, any>;
  preview?: Preview | null;
  warnings?: string[];
}

const CREATE: StagedAction = {
  id: 1, type: "CREATE", payload: { title: "notes/Sourdough", summary: "Add a sourdough note." },
  preview: { kind: "text", before: null, after: "# Sourdough\n\nFeed the starter daily." },
};
const EDIT: StagedAction = {
  id: 2, type: "UPDATE", payload: { title: "notes/Groceries" },
  preview: { kind: "text", before: "Milk\nEggs", after: "Milk\nEggs\nButter", label: "Groceries list" },
};
const DELETE: StagedAction = {
  id: 3, type: "DELETE", payload: { title: "notes/Obsolete" },
  preview: { kind: "text", before: "# Obsolete\n\nstale body", after: null },
};

// --- recording mutation handlers ------------------------------------------- //
let posted: { url: string; body: any }[] = [];

function stagingGet(actions: StagedAction[]) {
  return http.get("*/api/staging", () => HttpResponse.json(actions));
}
function recordPost(suffix: string, response: any = { ok: true }, status = 200) {
  return http.post(`*/api/staging/${suffix}`, async ({ request }) => {
    let body: any = null;
    try { body = await request.json(); } catch { /* no body */ }
    posted.push({ url: request.url, body });
    return HttpResponse.json(response, { status });
  });
}

beforeEach(() => {
  posted = [];
  vi.stubGlobal("alert", vi.fn());
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPanel(actions: StagedAction[], extra = [] as ReturnType<typeof http.post>[]) {
  const onChange = vi.fn();
  server.use(stagingGet(actions), ...extra);
  const utils = renderWithProviders(<StagingPanel tick={0} onChange={onChange} />);
  return { ...utils, onChange };
}

const applyCalls = (id: number) => posted.filter((p) => p.url.includes(`/staging/${id}/apply`));
const rejectCalls = (id: number) => posted.filter((p) => p.url.includes(`/staging/${id}/reject`));
const applyAllCalls = () => posted.filter((p) => p.url.includes("/staging/apply-all"));

// ---------------------------------------------------------------------------- //
// (a) rendering staged actions
// ---------------------------------------------------------------------------- //
describe("StagingPanel — rendering", () => {
  it("renders create / edit / delete cards with type tags, titles, and the pending count", async () => {
    renderPanel([CREATE, EDIT, DELETE]);

    // Header reflects the staged count.
    await screen.findByText(/3 pending/i);

    // One card per action: the type tag + the resolved title (preview.label wins over payload).
    expect(screen.getByText("CREATE")).toBeInTheDocument();
    expect(screen.getByText("UPDATE")).toBeInTheDocument();
    expect(screen.getByText("DELETE")).toBeInTheDocument();
    expect(screen.getByText("notes/Sourdough")).toBeInTheDocument();
    expect(screen.getByText("Groceries list")).toBeInTheDocument(); // preview.label
    expect(screen.getByText("notes/Obsolete")).toBeInTheDocument();

    // The create card surfaces its payload summary.
    expect(screen.getByText("Add a sourdough note.")).toBeInTheDocument();
  });

  it("shows a compact preview stat per card and reveals the inline diff on demand", async () => {
    const { user } = renderPanel([CREATE]);
    await screen.findByText("CREATE");

    // A "new" stat (before==null) appears on the collapsed header.
    expect(screen.getByText("new")).toBeInTheDocument();

    // The diff is hidden until "See changes" is clicked.
    expect(screen.queryByText(/Feed the starter daily/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /See changes/i }));
    await screen.findByText(/Feed the starter daily/i);

    // Toggle collapses it again.
    await user.click(screen.getByRole("button", { name: /Hide changes/i }));
    await waitFor(() =>
      expect(screen.queryByText(/Feed the starter daily/i)).not.toBeInTheDocument());
  });

  it("renders warnings and stale/conflict badges", async () => {
    const stale: StagedAction = {
      id: 4, type: "UPDATE", payload: { title: "notes/Risky" },
      preview: { kind: "text", before: "a", after: "b", stale: true, conflict: "edited elsewhere" },
      warnings: ["This overwrites a recent change"],
    };
    renderPanel([stale]);
    await screen.findByText("UPDATE");

    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.getByText("This overwrites a recent change")).toBeInTheDocument();
    // The conflict reason renders (appears both as a badge title and an inline line).
    expect(screen.getAllByText(/edited elsewhere/i).length).toBeGreaterThan(0);
  });

  it("renders nothing when there are no staged actions (empty state)", async () => {
    const { container, onChange } = renderPanel([]);
    // The GET resolves to [] and the panel returns null — nothing in the DOM.
    await waitFor(() => expect(container).toBeEmptyDOMElement());
    expect(onChange).not.toHaveBeenCalled();
  });

  it("renders nothing (and swallows the error) when the staging fetch fails", async () => {
    const { container } = renderPanel([], [
      http.get("*/api/staging", () => HttpResponse.json({ detail: "boom" }, { status: 500 })) as any,
    ]);
    // load() catches and leaves actions empty → null render, no throw, no alert.
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});

// ---------------------------------------------------------------------------- //
// (b) per-action Apply / Reject
// ---------------------------------------------------------------------------- //
describe("StagingPanel — apply / reject one action", () => {
  it("Apply posts to /staging/:id/apply, fires onChange, and re-loads (card clears)", async () => {
    // After the first GET returns the action, the post-apply reload returns []. A tiny
    // state machine flips the GET response so the success re-load empties the list.
    const state = { actions: [CREATE] as StagedAction[] };
    const onChange = vi.fn();
    server.use(
      http.get("*/api/staging", () => HttpResponse.json(state.actions)),
      http.post("*/api/staging/1/apply", async ({ request }) => {
        posted.push({ url: request.url, body: null });
        state.actions = []; // server applied it → no longer staged
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<StagingPanel tick={0} onChange={onChange} />);
    await screen.findByText("CREATE");

    await user.click(within(screen.getByText("CREATE").closest(".card")!).getByRole("button", { name: /^Apply$/ }));

    await waitFor(() => expect(applyCalls(1)).toHaveLength(1));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    // The reload picked up the now-empty list → the card is gone.
    await waitFor(() => expect(screen.queryByText("CREATE")).not.toBeInTheDocument());
    expect(rejectCalls(1)).toHaveLength(0);
  });

  it("Reject posts to /staging/:id/reject and fires onChange (apply never called)", async () => {
    const { user, onChange } = renderPanel([EDIT], [recordPost("2/reject") as any]);
    await screen.findByText("UPDATE");

    await user.click(screen.getByRole("button", { name: /^Reject$/ }));

    await waitFor(() => expect(rejectCalls(2)).toHaveLength(1));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    expect(applyCalls(2)).toHaveLength(0);
  });

  it("alerts on an apply failure but still re-loads + fires onChange", async () => {
    const alertFn = vi.fn();
    vi.stubGlobal("alert", alertFn);
    const { user, onChange } = renderPanel([DELETE], [
      recordPost("3/apply", { detail: "apply refused: stale" }, 409) as any,
    ]);
    await screen.findByText("DELETE");

    await user.click(within(screen.getByText("DELETE").closest(".card")!).getByRole("button", { name: /^Apply$/ }));

    await waitFor(() => expect(applyCalls(3)).toHaveLength(1));
    // run() surfaces the error message via alert(), then still runs the finally block.
    await waitFor(() => expect(alertFn).toHaveBeenCalledWith("apply refused: stale"));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
  });
});

// ---------------------------------------------------------------------------- //
// (c) Apply all
// ---------------------------------------------------------------------------- //
describe("StagingPanel — apply all", () => {
  it("Apply all posts to /staging/apply-all and reflects success by clearing the list", async () => {
    const state = { actions: [CREATE, EDIT] as StagedAction[] };
    const onChange = vi.fn();
    server.use(
      http.get("*/api/staging", () => HttpResponse.json(state.actions)),
      http.post("*/api/staging/apply-all", async ({ request }) => {
        posted.push({ url: request.url, body: null });
        state.actions = [];
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<StagingPanel tick={0} onChange={onChange} />);
    await screen.findByText(/2 pending/i);

    await user.click(screen.getByRole("button", { name: /Apply all/i }));

    await waitFor(() => expect(applyAllCalls()).toHaveLength(1));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText("CREATE")).not.toBeInTheDocument());
  });
});

// ---------------------------------------------------------------------------- //
// (d) the full-screen Review modal
// ---------------------------------------------------------------------------- //
describe("StagingPanel — review modal", () => {
  it("opens a per-action review modal with the full diff, then Apply from the modal posts", async () => {
    const { user } = renderPanel([EDIT], [recordPost("2/apply") as any]);
    await screen.findByText("UPDATE");

    await user.click(screen.getByRole("button", { name: /Review/i }));

    // The modal shows the title + a Close affordance.
    const modal = await screen.findByRole("button", { name: /^Close$/i });
    expect(modal).toBeInTheDocument();
    // The modal's full diff renders the proposed addition.
    await screen.findByText(/Butter/);

    // Applying from the modal footer hits the apply endpoint and dismisses the modal.
    const foot = document.querySelector(".modal-foot")!;
    await user.click(within(foot as HTMLElement).getByRole("button", { name: /^Apply$/ }));

    await waitFor(() => expect(applyCalls(2)).toHaveLength(1));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /^Close$/i })).not.toBeInTheDocument());
  });
});
