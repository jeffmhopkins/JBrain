// Component-integration (Tier 2) tests for TalkPanel — the article "AI talk"
// maintenance conversation (Wikipedia-Talk-style) that the KB maintenance pass
// reads and writes. Covers: loading existing talk history, adding an item
// (incl. a Correction promoted to a truth note), replying to an item (the
// "feed the next pass" path), dismissing an item, running an on-demand
// maintenance pass and surfacing its outcome/errors, empty/disabled states,
// and the resolved/minor fold affordances.
//
// Network: everything goes through MSW (server.use per test). setup.ts runs
// onUnhandledRequest:"error", so every endpoint the panel hits is mocked. The
// LLM capability that gates "Resolve with AI now" is seeded into the REAL
// health store via ingestVerify() — TalkPanel reads it through useCapability().
//
// NB: TalkPanel's reply path (talkReply) is a plain POST followed by a reload,
// not an SSE stream, so there is no api-module mock here — MSW carries it and
// we assert the request body + the re-rendered DOM, mirroring how Chat.test.tsx
// asserts requests + DOM for its non-stream flows.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { __reset, ingestVerify } from "../health";

import TalkPanel from "./TalkPanel";

const READY_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: false } },
  embeddings: { state: "ready" }, transcription: { state: "ready" },
  push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
};
function caps(over: Record<string, any> = {}): any {
  return { capabilities: { ...READY_CAPS, ...over } };
}

interface Reply { id: number; author: string; body: string; created_at: string; }
interface Talk {
  id: number; kind: string; body: string; author: string; created_at: string;
  resolved_at: string | null; resolution?: string | null;
  is_correction?: number; source_note_slug?: string | null;
  replies?: Reply[];
}

function talk(over: Partial<Talk> = {}): Talk {
  return {
    id: 1, kind: "directive", body: "Keep this concise.", author: "ai",
    created_at: "2024-01-01T00:00:00Z", resolved_at: null, replies: [],
    ...over,
  };
}

// Track the JSON write endpoints the panel hits (URL + parsed body) so tests can
// assert routing without reaching into component internals.
let posted: { url: string; method: string; body: any }[] = [];

// Register the GET that every mount performs (the talk history). `state.items`
// is the live list, so a handler that mutates it makes the post-write reload
// observe the change — exactly what the component does.
function mountHandlers(initial: Talk[]) {
  const state = { items: initial };
  // NB: the slug is multi-segment (e.g. "kb/people/jeff") and goes into the URL
  // verbatim, so `:slug` (single-segment) won't match — use a `*` wildcard.
  server.use(
    http.get("*/api/notes/*/talk", () => HttpResponse.json(state.items)),
  );
  return state;
}

beforeEach(() => {
  __reset();
  posted = [];
  server.resetHandlers();
  ingestVerify(caps());            // LLM ready by default → "Resolve with AI now" enabled
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------- //
// (a) Loading existing talk history
// ---------------------------------------------------------------------------- //
describe("TalkPanel — load history", () => {
  it("renders open primary items with their replies", async () => {
    mountHandlers([
      talk({ id: 1, kind: "conflict", body: "Two birthdays on file." }),
      talk({
        id: 2, kind: "question", body: "Which spelling is right?",
        replies: [
          { id: 10, author: "user", body: "Use Bjørn.", created_at: "2024-01-02T00:00:00Z" },
          { id: 11, author: "ai", body: "Noted — applied.", created_at: "2024-01-03T00:00:00Z" },
        ],
      }),
    ]);
    renderWithProviders(<TalkPanel slug="kb/people/jeff" />);

    expect(await screen.findByText("Two birthdays on file.")).toBeInTheDocument();
    expect(screen.getByText("Which spelling is right?")).toBeInTheDocument();
    // The item's replies render with the You/AI attribution.
    expect(screen.getByText("Use Bjørn.")).toBeInTheDocument();
    expect(screen.getByText("Noted — applied.")).toBeInTheDocument();
    expect(screen.getByText("AI:")).toBeInTheDocument();
    expect(screen.getByText("You:")).toBeInTheDocument();
  });

  it("shows the empty state when there are no open items", async () => {
    mountHandlers([]);
    renderWithProviders(<TalkPanel slug="kb/empty" />);
    expect(await screen.findByText(/No open items\./i)).toBeInTheDocument();
    // No open items ⇒ the on-demand maintenance row is hidden.
    expect(screen.queryByRole("button", { name: /Resolve with AI now/i })).not.toBeInTheDocument();
  });

  it("falls back to empty when the history request fails", async () => {
    server.use(
      http.get("*/api/notes/*/talk", () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
    );
    renderWithProviders(<TalkPanel slug="kb/broken" />);
    expect(await screen.findByText(/No open items\./i)).toBeInTheDocument();
  });

  it("folds inert note/decision logs behind a summary and resolved items behind another", async () => {
    mountHandlers([
      talk({ id: 1, kind: "directive", body: "An open directive." }),
      talk({ id: 2, kind: "note", body: "An inert log line.", author: "ai" }),
      talk({ id: 3, kind: "decision", body: "Picked option A.", author: "ai" }),
      talk({ id: 4, kind: "todo", body: "A done todo.", resolved_at: "2024-02-01T00:00:00Z", resolution: "Handled." }),
    ]);
    renderWithProviders(<TalkPanel slug="kb/folds" />);

    expect(await screen.findByText("An open directive.")).toBeInTheDocument();
    // Two inert logs collapse into a "2 notes" disclosure.
    expect(screen.getByText("2 notes")).toBeInTheDocument();
    // One resolved item collapses into a "1 resolved" disclosure, with its resolution.
    expect(screen.getByText("1 resolved")).toBeInTheDocument();
    expect(screen.getByText("A done todo.")).toBeInTheDocument();
    expect(screen.getByText(/→ Handled\./)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) Adding an item (directive + correction-promoted-to-truth-note)
// ---------------------------------------------------------------------------- //
describe("TalkPanel — add item", () => {
  it("posts the typed directive and reloads to show it", async () => {
    const state = mountHandlers([]);
    server.use(
      http.post("*/api/notes/*/talk", async ({ request }) => {
        const body = await request.json();
        posted.push({ url: request.url, method: "POST", body });
        // Server appends and the panel's reload then renders it.
        state.items = [talk({ id: 5, kind: (body as any).kind, body: (body as any).body, author: "user" })];
        return HttpResponse.json({ id: 5 });
      }),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText(/No open items\./i);

    await user.type(screen.getByPlaceholderText(/Add a note for the next pass/i), "Trim the intro.");
    await user.click(screen.getByRole("button", { name: /^Add$/i }));

    await waitFor(() => expect(posted).toHaveLength(1));
    // add() builds the path with the raw slug (no encodeURIComponent), so the
    // slash stays literal.
    expect(posted[0].url).toMatch(/\/api\/notes\/kb\/x\/talk$/);
    expect(posted[0].body).toEqual({ kind: "directive", body: "Trim the intro." });
    // The reload surfaces the new item (tagged "— you").
    expect(await screen.findByText("Trim the intro.")).toBeInTheDocument();
    expect(screen.getByText(/— you/)).toBeInTheDocument();
  });

  it("does nothing on an empty/whitespace body", async () => {
    mountHandlers([]);
    let calls = 0;
    server.use(
      http.post("*/api/notes/*/talk", () => { calls++; return HttpResponse.json({ id: 9 }); }),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText(/No open items\./i);

    await user.type(screen.getByPlaceholderText(/Add a note for the next pass/i), "   ");
    await user.click(screen.getByRole("button", { name: /^Add$/i }));
    // No request fired — the guard short-circuits on a blank body.
    expect(calls).toBe(0);
  });

  it("flashes the truth-note confirmation when a correction is promoted", async () => {
    const state = mountHandlers([]);
    server.use(
      http.post("*/api/notes/*/talk", async ({ request }) => {
        posted.push({ url: request.url, method: "POST", body: await request.json() });
        state.items = [talk({ id: 7, kind: "correction", body: "Spelled Bjørn.", author: "user",
          is_correction: 1, source_note_slug: "kb/truth/bjorn" })];
        return HttpResponse.json({ id: 7, promoted: { slug: "kb/truth/bjorn", title: "Bjørn (truth)" } });
      }),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText(/No open items\./i);

    // Switch the kind selector to Correction, which also swaps the placeholder.
    await user.selectOptions(screen.getByRole("combobox"), "correction");
    await user.type(screen.getByPlaceholderText(/State the correct fact/i), "Spelled Bjørn.");
    await user.click(screen.getByRole("button", { name: /^Add$/i }));

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0].body).toEqual({ kind: "correction", body: "Spelled Bjørn." });
    // The promotion flash + the correction's truth-note badge both render.
    expect(await screen.findByText(/Saved as a source-of-truth note \(Bjørn \(truth\)\)/i)).toBeInTheDocument();
    const badge = screen.getByRole("link", { name: /truth note/i });
    expect(badge).toHaveAttribute("href", "/note/kb/truth/bjorn");
  });
});

// ---------------------------------------------------------------------------- //
// (c) Replying to an item — the "feed the next synthesis pass" path
// ---------------------------------------------------------------------------- //
describe("TalkPanel — reply", () => {
  it("opens the composer, posts the reply, and shows it after reload", async () => {
    const state = mountHandlers([talk({ id: 3, kind: "question", body: "Confirm the date?" })]);
    server.use(
      http.post("*/api/notes/*/talk/:id/reply", async ({ request }) => {
        posted.push({ url: request.url, method: "POST", body: await request.json() });
        state.items = [talk({ id: 3, kind: "question", body: "Confirm the date?",
          replies: [{ id: 99, author: "user", body: "Yes, Jan 3.", created_at: "2024-01-04T00:00:00Z" }] })];
        return HttpResponse.json({ id: 99 });
      }),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("Confirm the date?");

    await user.click(screen.getByRole("button", { name: /^Reply$/i }));
    const input = screen.getByPlaceholderText(/Reply — read by the next maintenance pass/i);
    await user.type(input, "Yes, Jan 3.");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0].url).toMatch(/\/talk\/3\/reply$/);
    expect(posted[0].body).toEqual({ body: "Yes, Jan 3." });
    // The reload renders the new reply and closes the composer.
    expect(await screen.findByText("Yes, Jan 3.")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Reply — read by the next maintenance pass/i)).not.toBeInTheDocument();
  });

  it("disables Send until the reply has non-whitespace text", async () => {
    mountHandlers([talk({ id: 3, body: "Steer me." })]);
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("Steer me.");

    await user.click(screen.getByRole("button", { name: /^Reply$/i }));
    // Re-query each time: ReplyBox is an inline component, so a state change
    // remounts it and the old button node goes stale.
    expect((screen.getByRole("button", { name: /^Send$/i }) as HTMLButtonElement).disabled).toBe(true);
    await user.type(screen.getByPlaceholderText(/Reply — read by the next maintenance pass/i), "ok");
    await waitFor(() =>
      expect((screen.getByRole("button", { name: /^Send$/i }) as HTMLButtonElement).disabled).toBe(false),
    );
  });
});

// ---------------------------------------------------------------------------- //
// (d) Dismissing an item (owner judgement; refused on corrections)
// ---------------------------------------------------------------------------- //
describe("TalkPanel — dismiss", () => {
  it("confirms, posts the dismiss, and reloads without the item", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const state = mountHandlers([talk({ id: 4, kind: "todo", body: "Stale todo." })]);
    server.use(
      http.post("*/api/notes/*/talk/:id/dismiss", async ({ request }) => {
        posted.push({ url: request.url, method: "POST", body: await request.json() });
        state.items = [];   // removed → next reload shows the empty state
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("Stale todo.");

    await user.click(screen.getByRole("button", { name: /^Dismiss$/i }));

    expect(confirmSpy).toHaveBeenCalledOnce();
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0].url).toMatch(/\/talk\/4\/dismiss$/);
    await screen.findByText(/No open items\./i);
  });

  it("does not post when the confirm dialog is cancelled", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    mountHandlers([talk({ id: 4, kind: "todo", body: "Stale todo." })]);
    let calls = 0;
    server.use(
      http.post("*/api/notes/*/talk/:id/dismiss", () => { calls++; return HttpResponse.json({ ok: true }); }),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("Stale todo.");
    await user.click(screen.getByRole("button", { name: /^Dismiss$/i }));
    expect(calls).toBe(0);
    expect(screen.getByText("Stale todo.")).toBeInTheDocument();
  });

  it("alerts when the dismiss request fails", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    mountHandlers([talk({ id: 4, kind: "todo", body: "Stale todo." })]);
    server.use(
      http.post("*/api/notes/*/talk/:id/dismiss", () =>
        HttpResponse.json({ detail: "could not dismiss" }, { status: 500 })),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("Stale todo.");
    await user.click(screen.getByRole("button", { name: /^Dismiss$/i }));

    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("could not dismiss"));
    // The item stays since the reload still returns it.
    expect(screen.getByText("Stale todo.")).toBeInTheDocument();
  });

  it("offers no Dismiss on a correction item", async () => {
    mountHandlers([talk({ id: 4, kind: "correction", body: "A fact fix.", author: "user", is_correction: 1 })]);
    renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("A fact fix.");
    expect(screen.queryByRole("button", { name: /^Dismiss$/i })).not.toBeInTheDocument();
    // Reply is still offered.
    expect(screen.getByRole("button", { name: /^Reply$/i })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (e) On-demand maintenance pass — outcome + error surfacing, capability gating
// ---------------------------------------------------------------------------- //
describe("TalkPanel — maintain now", () => {
  it("runs the pass and reports a change + resolved count", async () => {
    const state = mountHandlers([talk({ id: 1, kind: "directive", body: "Tighten it up." })]);
    server.use(
      http.post("*/api/notes/*/talk/maintain-now", ({ request }) => {
        posted.push({ url: request.url, method: "POST", body: null });
        // The pass resolved a sibling item but this directive stays open, so the
        // maintenance row (which hosts the outcome message) stays mounted.
        return HttpResponse.json({ ok: true, title: "Article", changed: true, resolved: 1 });
      }),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("Tighten it up.");

    await user.click(screen.getByRole("button", { name: /Resolve with AI now/i }));

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0].url).toMatch(/\/talk\/maintain-now$/);
    expect(await screen.findByText(/Updated the article · resolved 1\./i)).toBeInTheDocument();
  });

  it("reports a no-change pass with the kept-open count", async () => {
    mountHandlers([talk({ id: 1, body: "Open item." })]);
    server.use(
      http.post("*/api/notes/*/talk/maintain-now", () =>
        HttpResponse.json({ ok: true, title: "Article", changed: false, kept_open: 2 })),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("Open item.");
    await user.click(screen.getByRole("button", { name: /Resolve with AI now/i }));
    expect(await screen.findByText(/no change needed · 2 still open/i)).toBeInTheDocument();
  });

  it("surfaces a not-ok reason from the pass", async () => {
    mountHandlers([talk({ id: 1, body: "Open item." })]);
    server.use(
      http.post("*/api/notes/*/talk/maintain-now", () =>
        HttpResponse.json({ ok: false, title: "Article", reason: "Already running." })),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("Open item.");
    await user.click(screen.getByRole("button", { name: /Resolve with AI now/i }));
    expect(await screen.findByText(/Already running\./i)).toBeInTheDocument();
  });

  it("surfaces a thrown error from the pass", async () => {
    mountHandlers([talk({ id: 1, body: "Open item." })]);
    server.use(
      http.post("*/api/notes/*/talk/maintain-now", () =>
        HttpResponse.json({ detail: "server exploded" }, { status: 500 })),
    );
    const { user } = renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("Open item.");
    await user.click(screen.getByRole("button", { name: /Resolve with AI now/i }));
    expect(await screen.findByText(/server exploded/i)).toBeInTheDocument();
  });

  it("disables the pass button and explains why when the LLM is absent", async () => {
    __reset();
    ingestVerify(caps({ llm: { state: "absent", providers: { anthropic: false, xai: false } } }));
    mountHandlers([talk({ id: 1, body: "Open item." })]);
    renderWithProviders(<TalkPanel slug="kb/x" />);
    await screen.findByText("Open item.");

    const btn = screen.getByRole("button", { name: /Resolve with AI now/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    // The capability reason rides on the button's title attribute.
    expect(btn.title).toMatch(/need an API key/i);
  });
});
