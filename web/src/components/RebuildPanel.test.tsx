// Component-integration (Tier 2) tests for RebuildPanel.tsx — the KB-rebuild client
// state machine. It drives a two-stage gather → curate → draft flow over SSE, plus
// source selection and accept/reject.
//
// Network: the plain JSON endpoints (accept / reject / add-source search) go through
// MSW (server.use per test, recorded so routing can be asserted). The SSE streaming
// calls are awkward over MSW, so they're partially mocked here exactly like
// Chat.test.tsx mocks streamChat: vi.mock("../api") keeps every export real and only
// swaps the five stream helpers for scriptable fakes. Each fake records its call,
// returns a real-shaped SSEHandle ({ done, abort }), and drives events onto the live
// onEvent callback so the gather → curate → draft transitions are deterministic.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import type { RebuildEvent, SSEHandle } from "../api";

// --- stream fakes ----------------------------------------------------------- //
// Per-stream scripts the mocked helpers delegate to. A script receives the live
// onEvent and emits whatever sequence a test wants; it resolves the handle's `done`
// when it returns. Default: a no-op (resolves immediately, no events).
type Script = (onEvent: (e: RebuildEvent) => void) => void | Promise<void>;
type Call = { kind: string; args: any[] };

const calls: Call[] = [];
const scripts: Record<string, Script> = {};
const abortSpy = vi.fn();

function fakeStream(kind: string, onEvent: (e: RebuildEvent) => void, args: any[]): SSEHandle {
  calls.push({ kind, args });
  const script = scripts[kind] ?? (() => {});
  // Run the script on a microtask so the component finishes its render/commit before
  // events arrive (mirrors a real network round-trip), then resolve `done`.
  const done = Promise.resolve().then(() => script(onEvent));
  return { done, abort: abortSpy };
}

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    rebuildStream: vi.fn((slug: string, onEvent: any) =>
      fakeStream("rebuild", onEvent, [slug])),
    regatherStream: vi.fn((runId: string, hint: string, onEvent: any) =>
      fakeStream("regather", onEvent, [runId, hint])),
    draftStream: vi.fn((runId: string, sourceIds: number[], onEvent: any) =>
      fakeStream("draft", onEvent, [runId, sourceIds])),
    redraftStream: vi.fn((runId: string, maxTokens: number, onEvent: any) =>
      fakeStream("redraft", onEvent, [runId, maxTokens])),
    guideStream: vi.fn((runId: string, text: string, onEvent: any) =>
      fakeStream("guide", onEvent, [runId, text])),
  };
});

// Imported AFTER the mock factory (vi.mock is hoisted above all imports).
import RebuildPanel from "./RebuildPanel";
import { __reset } from "../health";

const RUN_ID = "run-123";
const SLUG = "kb-bread";
const NOTE = { title: "kb/Bread", content_md: "# Bread\n\nOld body." };

// A canned gather → sources_proposed script: two candidates (both on), one skipped.
function gatherWithSources(extra: Partial<{ skipped: boolean }> = {}): Script {
  return (onEvent) => {
    onEvent({ type: "run_started", run_id: RUN_ID, slug: SLUG, title: "kb/Bread", base_rev: "r1" });
    onEvent({ type: "tool_use", tool: "search_notes", query: "bread" });
    onEvent({ type: "tool_result", tool: "search_notes", summary: "found 2", items: ["notes/sourdough"] });
    onEvent({
      type: "sources_proposed",
      candidates: [
        { note_id: 1, title: "notes/Sourdough", date: "2024-01-01", reason: "core technique", on: true, private: false },
        { note_id: 2, title: "notes/Rye", date: "2024-02-01", reason: "variant", on: true, private: false },
      ],
      skipped: extra.skipped
        ? [{ note_id: 9, title: "notes/Random", date: "2023-01-01", reason: "off topic" }]
        : [],
    });
  };
}

// --- recording JSON handlers ------------------------------------------------ //
let posted: { url: string; method: string; body: any }[] = [];

function recordingHandlers() {
  return [
    http.post(`*/api/kb/rebuild/${RUN_ID}/accept`, async ({ request }) => {
      posted.push({ url: request.url, method: "POST", body: await request.json() });
      return HttpResponse.json({ ok: true, slug: "kb-bread-v2" });
    }),
    http.post(`*/api/kb/rebuild/${RUN_ID}/reject`, async ({ request }) => {
      posted.push({ url: request.url, method: "POST", body: null });
      return HttpResponse.json({ ok: true });
    }),
    http.get(`*/api/kb/rebuild/${RUN_ID}/search`, ({ request }) => {
      posted.push({ url: request.url, method: "GET", body: null });
      return HttpResponse.json([{ note_id: 7, title: "notes/Bagel", date: "2024-03-01" }]);
    }),
  ];
}

const acceptCalls = () => posted.filter((p) => p.url.includes("/accept"));
const rejectCalls = () => posted.filter((p) => p.url.includes("/reject"));

// The Modal header always renders its own aria-label="Close" button; the rebuild
// footer adds a separate "Close" action in terminal phases. Scope footer-button
// queries to .modal-foot so they never collide with the header's Close.
function footBtn(re: RegExp): HTMLButtonElement {
  const foot = document.querySelector(".modal-foot")!;
  const btn = Array.from(foot.querySelectorAll("button")).find((b) => re.test(b.textContent || ""));
  if (!btn) throw new Error(`no footer button matching ${re}`);
  return btn as HTMLButtonElement;
}

beforeEach(() => {
  __reset();
  posted = [];
  calls.length = 0;
  abortSpy.mockClear();
  for (const k of Object.keys(scripts)) delete scripts[k];
  // jsdom has no native confirm/alert — requestClose() and the accept-error path use them.
  vi.stubGlobal("confirm", vi.fn(() => true));
  vi.stubGlobal("alert", vi.fn());
  server.use(...recordingHandlers());
});
afterEach(() => cleanup());

function renderPanel(over: Partial<React.ComponentProps<typeof RebuildPanel>> = {}) {
  const onClose = vi.fn();
  const onAccepted = vi.fn();
  const utils = renderWithProviders(
    <RebuildPanel slug={SLUG} note={NOTE} onClose={onClose} onAccepted={onAccepted} {...over} />,
  );
  return { ...utils, onClose, onAccepted };
}

// ---------------------------------------------------------------------------- //
// (a) Mount → gather kicks off automatically
// ---------------------------------------------------------------------------- //
describe("RebuildPanel — gather (Stage 1)", () => {
  it("opens the rebuild stream on mount and shows gathering progress", async () => {
    scripts.rebuild = (onEvent) => {
      onEvent({ type: "run_started", run_id: RUN_ID, slug: SLUG, title: "kb/Bread", base_rev: "r1" });
      onEvent({ type: "tool_use", tool: "search_notes", query: "bread" });
    };
    renderPanel();

    // The gather stream is opened on mount with the slug.
    await waitFor(() => expect(calls.some((c) => c.kind === "rebuild")).toBe(true));
    expect(calls.find((c) => c.kind === "rebuild")!.args[0]).toBe(SLUG);

    // A streamed tool_use surfaces its friendly label (shown both as the live status
    // line and the step row) and the queried step.
    await waitFor(() => expect(screen.getAllByText(/Searching your notes/i).length).toBeGreaterThan(0));
    expect(screen.getByText(/“bread”/)).toBeInTheDocument();
  });

  it("surfaces a mid-gather error event in place", async () => {
    scripts.rebuild = (onEvent) => {
      onEvent({ type: "run_started", run_id: RUN_ID, slug: SLUG, title: "kb/Bread", base_rev: "r1" });
      onEvent({ type: "error", message: "gather engine exploded" });
    };
    renderPanel();
    await screen.findByText(/gather engine exploded/i);
    // The error footer offers a single Close, not Cancel.
    expect(footBtn(/^Close$/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cancel/i })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) gather → curate: candidates render + selection toggles
// ---------------------------------------------------------------------------- //
describe("RebuildPanel — curate (Stage 2)", () => {
  it("advances to the curate stage and renders proposed candidates", async () => {
    scripts.rebuild = gatherWithSources({ skipped: true });
    renderPanel();

    // "Review sources" is both the modal title and the stage-2 pip label.
    await waitFor(() => expect(screen.getAllByText(/Review sources/i).length).toBeGreaterThan(0));
    expect(screen.getByText(/Sources found \(2\)/i)).toBeInTheDocument();
    expect(screen.getByText("Sourdough")).toBeInTheDocument();
    expect(screen.getByText("Rye")).toBeInTheDocument();

    // Both candidates default-on → the draft button reflects a count of 2.
    expect(screen.getByRole("button", { name: /Draft from 2 sources/i })).toBeInTheDocument();

    // The skipped section is collapsible and reveals the skipped note.
    await screen.findByText(/Considered but skipped \(1\)/i);
  });

  it("toggling a source off updates the selected count, and zero blocks drafting", async () => {
    scripts.rebuild = gatherWithSources();
    const { user } = renderPanel();
    await screen.findByText("Sourdough");

    // Toggle both off (rows are clickable).
    await user.click(screen.getByText("Sourdough"));
    expect(screen.getByRole("button", { name: /Draft from 1 source$/i })).toBeInTheDocument();

    await user.click(screen.getByText("Rye"));
    const blocked = screen.getByRole("button", { name: /Pick at least one source/i }) as HTMLButtonElement;
    expect(blocked.disabled).toBe(true);

    // A no-selection click never opens a draft stream.
    await user.click(blocked);
    expect(calls.some((c) => c.kind === "draft")).toBe(false);
  });
});

// ---------------------------------------------------------------------------- //
// (c) curate → draft: streamed draft renders
// ---------------------------------------------------------------------------- //
describe("RebuildPanel — draft (Stage 3)", () => {
  async function gotoDraft(draftScript: Script) {
    scripts.rebuild = gatherWithSources();
    scripts.draft = draftScript;
    const utils = renderPanel();
    await screen.findByRole("button", { name: /Draft from 2 sources/i });
    await utils.user.click(screen.getByRole("button", { name: /Draft from 2 sources/i }));
    return utils;
  }

  it("starts a draft stream with the selected source ids and renders streamed content", async () => {
    await gotoDraft((onEvent) => {
      onEvent({ type: "content_delta", text: "# Bread\n\nNew " });
      onEvent({ type: "content_delta", text: "body." });
      onEvent({ type: "done", draft: "# Bread\n\nNew body." });
    });

    // The draft stream fired with the two selected note_ids.
    await waitFor(() => expect(calls.some((c) => c.kind === "draft")).toBe(true));
    expect(calls.find((c) => c.kind === "draft")!.args[1]).toEqual([1, 2]);

    // Streamed body renders; review-phase actions appear.
    await screen.findByText(/New body\./i);
    await screen.findByRole("button", { name: /^Accept$/i });
    expect(screen.getByRole("button", { name: /^Reject$/i })).toBeInTheDocument();
  });

  it("a mid-draft error event flips to the error phase with a Close action", async () => {
    await gotoDraft((onEvent) => {
      onEvent({ type: "content_delta", text: "partial" });
      onEvent({ type: "error", message: "model timed out" });
    });

    await screen.findByText(/^Failed$/i);
    // Error phase: no Accept/Reject, just a single Close in the footer. (The Modal
    // header has its own aria-label="Close" button, so scope to .modal-foot.)
    expect(screen.queryByRole("button", { name: /^Accept$/i })).not.toBeInTheDocument();
    expect(footBtn(/^Close$/)).toBeInTheDocument();
  });

  it("a truncated draft warns and offers re-draft with more room", async () => {
    const utils = await gotoDraft((onEvent) => {
      onEvent({ type: "done", draft: "# Bread\n\nCut off here", truncated: true });
    });
    await screen.findByText(/cut short/i);

    scripts.redraft = (onEvent) => onEvent({ type: "done", draft: "# Bread\n\nFull length now." });
    await utils.user.click(screen.getByRole("button", { name: /Re-draft with more room/i }));

    // Re-draft opens a redraft stream at a larger budget.
    await waitFor(() => expect(calls.some((c) => c.kind === "redraft")).toBe(true));
    expect(calls.find((c) => c.kind === "redraft")!.args[1]).toBe(10000);
    await screen.findByText(/Full length now\./i);
  });
});

// ---------------------------------------------------------------------------- //
// (d) accept / reject endpoints
// ---------------------------------------------------------------------------- //
describe("RebuildPanel — accept / reject", () => {
  async function gotoReview() {
    scripts.rebuild = gatherWithSources();
    scripts.draft = (onEvent) => onEvent({ type: "done", draft: "# Bread\n\nFinal draft." });
    const utils = renderPanel();
    await screen.findByRole("button", { name: /Draft from 2 sources/i });
    await utils.user.click(screen.getByRole("button", { name: /Draft from 2 sources/i }));
    await screen.findByRole("button", { name: /^Accept$/i });
    return utils;
  }

  it("Accept posts to the accept endpoint and reports the new slug", async () => {
    const { user, onAccepted } = await gotoReview();
    await user.click(screen.getByRole("button", { name: /^Accept$/i }));

    await waitFor(() => expect(acceptCalls()).toHaveLength(1));
    // No rename requested → rename_to is null.
    expect(acceptCalls()[0].body).toEqual({ rename_to: null });
    await waitFor(() => expect(onAccepted).toHaveBeenCalledWith("kb-bread-v2"));
  });

  it("Reject aborts the stream, posts to the reject endpoint, and closes", async () => {
    const { user, onClose } = await gotoReview();
    await user.click(screen.getByRole("button", { name: /^Reject$/i }));

    expect(abortSpy).toHaveBeenCalled();
    await waitFor(() => expect(rejectCalls()).toHaveLength(1));
    expect(onClose).toHaveBeenCalled();
    // Accept must not have fired on the reject path.
    expect(acceptCalls()).toHaveLength(0);
  });

  it("a 409 stale conflict on accept surfaces a stale warning instead of closing", async () => {
    server.use(
      http.post(`*/api/kb/rebuild/${RUN_ID}/accept`, () =>
        HttpResponse.json({ detail: "page is stale" }, { status: 409 })),
    );
    const { user, onAccepted } = await gotoReview();
    await user.click(screen.getByRole("button", { name: /^Accept$/i }));

    await screen.findByText(/changed since the rebuild started/i);
    expect(onAccepted).not.toHaveBeenCalled();
  });
});
