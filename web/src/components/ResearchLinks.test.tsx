// Component-integration (Tier 2) tests for <ResearchLinks> — the OWNER-side
// management surface for "research" share links: read-only, scoped Q&A over notes
// (and optionally a few lab analytes) that the owner approves. Creation happens via
// the assistant elsewhere; this component lists existing links and lets the owner
// activate / copy / revoke them and, via the Manage modal, approve candidate notes,
// attach labs, edit scope/persona/TTL settings, and audit sessions.
//
// Unlike the encrypted chat surface, research links carry NO client-side crypto: the
// server holds the (owner-scoped) data and the URL is a plain token. So there is no
// key-wrap round-trip to assert here — the invariants under test are which REQUESTS
// fire (and with what bodies) for each owner action, paired with the DOM.
//
// Network: every endpoint the component hits is mocked via MSW
// (onUnhandledRequest:"error"); each test layers in just the routes its flow needs.
// The Manage modal eagerly fetches researchDetail() + getLabAnalytes() on open, so
// those two are always provided once a modal is opened.
//
// Auth: the component (and its Modal / ConversationView / ShareOptions children) do
// not read useAuth(); we still stub ../App exactly as the sibling SharesPage tests do,
// to keep the established shape and guard against the App tree ever being pulled in.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";

vi.mock("../App", () => ({
  useAuth: () => ({ appTz: "UTC", brainName: "Test Brain" }),
  PWA_VERSION: "test",
}));

// Imported AFTER the mock factory (vi.mock is hoisted above imports).
import ResearchLinks from "./ResearchLinks";

interface RLink {
  id: number; label: string | null; url: string; spec_status: string;
  approved_count: number; lab_count: number; sessions: number;
  reply_count: number; max_total_replies: number;
}

// Records every mutating request a flow makes so we can assert routing + bodies
// without reaching into component internals.
let posted: { url: string; method: string; body: any }[] = [];

function link(over: Partial<RLink> = {}): RLink {
  return {
    id: 1, label: "Care team Q&A", url: "https://x/share/restok",
    spec_status: "active", approved_count: 2, lab_count: 0, sessions: 0,
    reply_count: 3, max_total_replies: 200, ...over,
  };
}

// A researchDetail() payload (the Manage modal's eager GET). Tests override slices.
function detailPayload(over: Record<string, unknown> = {}) {
  return {
    spec: {
      status: "active", persona_voice: "", topics: "", intro: "",
      bind: false, single_use: false, max_turns: 30, max_total_replies: 200,
    },
    approved: [],
    candidates: [],
    sessions: [],
    labs: { analytes: [], window_from: null, window_to: null },
    expires_at: null,
    bound: false,
    ...over,
  };
}

// Register the two GETs the Manage modal fires on open, capturing nothing.
function manageHandlers(detail: Record<string, unknown> = {}, analytes: unknown[] = []) {
  return [
    http.get("*/api/shares/research/:id", () => HttpResponse.json(detailPayload(detail))),
    http.get("*/api/medical/labs/analytes", () => HttpResponse.json({ analytes })),
  ];
}

beforeEach(() => {
  posted = [];
});
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) Listing existing research links.
// ---------------------------------------------------------------------------- //
describe("ResearchLinks — listing", () => {
  it("renders an active link with its url, live badge and usage", () => {
    renderWithProviders(
      <ResearchLinks
        links={[link({ id: 7, label: "For Dr. Lee", approved_count: 3, lab_count: 2, sessions: 1, reply_count: 5, max_total_replies: 200, url: "https://x/share/abc" })]}
        reload={() => {}}
      />,
    );

    expect(screen.getByText("For Dr. Lee")).toBeInTheDocument();
    expect(screen.getByText("live")).toBeInTheDocument();
    expect(screen.getByText("3 notes")).toBeInTheDocument();
    expect(screen.getByText("2 labs")).toBeInTheDocument();
    expect(screen.getByText("1 chat")).toBeInTheDocument();
    expect(screen.getByText("5/200 answers used")).toBeInTheDocument();
    // The (read-only) share URL is shown for an active link.
    expect(screen.getByDisplayValue("https://x/share/abc")).toBeInTheDocument();
    // Active links expose Copy / Manage / Revoke (no Activate).
    expect(screen.getByRole("button", { name: /^Revoke$/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Activate link/i })).not.toBeInTheDocument();
  });

  it("renders a draft link with the draft affordances (Activate / Delete, no url)", () => {
    renderWithProviders(
      <ResearchLinks
        links={[link({ id: 8, label: "Draft link", spec_status: "draft", approved_count: 1, url: "https://x/share/draft" })]}
        reload={() => {}}
      />,
    );

    expect(screen.getByText("draft — not live")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Activate link/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Delete$/i })).toBeInTheDocument();
    // A draft has no shareable url shown yet.
    expect(screen.queryByDisplayValue("https://x/share/draft")).not.toBeInTheDocument();
  });

  it("disables Activate for a draft with nothing approved (no notes, no labs)", () => {
    renderWithProviders(
      <ResearchLinks
        links={[link({ id: 9, spec_status: "draft", approved_count: 0, lab_count: 0 })]}
        reload={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: /Activate link/i })).toBeDisabled();
  });

  it("renders the section hint and nothing else when there are no links (empty)", () => {
    renderWithProviders(<ResearchLinks links={[]} reload={() => {}} />);
    expect(screen.getByText(/Read-only Q&A scoped to notes you approve/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Manage/i })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) Copy / revoke / activate from the list row.
// ---------------------------------------------------------------------------- //
describe("ResearchLinks — row actions", () => {
  it("copies the link url to the clipboard", async () => {
    const { user } = renderWithProviders(
      <ResearchLinks links={[link({ id: 3, url: "https://x/share/copyme" })]} reload={() => {}} />,
    );

    await user.click(screen.getByRole("button", { name: /^Copy$/i }));
    // user-event installs its own clipboard stub; read the written value back.
    await waitFor(async () =>
      expect(await navigator.clipboard.readText()).toBe("https://x/share/copyme"));
    // The button flips to its "Copied" confirmation state.
    await screen.findByRole("button", { name: /^Copied$/i });
  });

  it("revokes an active link — confirms, POSTs /revoke, then reloads", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const reload = vi.fn();
    server.use(
      http.post("*/api/shares/4/revoke", async ({ request }) => {
        posted.push({ url: request.url, method: "POST", body: await request.text() });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(
      <ResearchLinks links={[link({ id: 4 })]} reload={reload} />,
    );

    await user.click(screen.getByRole("button", { name: /^Revoke$/i }));

    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/4/revoke"))).toBe(true));
    // revoke() sends no body.
    expect(posted[0].body).toBe("");
    await waitFor(() => expect(reload).toHaveBeenCalled());
  });

  it("does NOT revoke when the confirm is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const reload = vi.fn();
    // No /revoke handler registered: if a request fired, MSW (onUnhandledRequest:"error")
    // would fail the test.
    const { user } = renderWithProviders(
      <ResearchLinks links={[link({ id: 5 })]} reload={reload} />,
    );

    await user.click(screen.getByRole("button", { name: /^Revoke$/i }));
    expect(reload).not.toHaveBeenCalled();
  });

  it("activates a draft link — POSTs /activate and reloads", async () => {
    const reload = vi.fn();
    server.use(
      http.post("*/api/shares/research/6/activate", async ({ request }) => {
        posted.push({ url: request.url, method: "POST", body: await request.text() });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(
      <ResearchLinks links={[link({ id: 6, spec_status: "draft", approved_count: 1 })]} reload={reload} />,
    );

    await user.click(screen.getByRole("button", { name: /Activate link/i }));

    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/research/6/activate"))).toBe(true));
    await waitFor(() => expect(reload).toHaveBeenCalled());
  });

  it("surfaces an alert when activation fails (server error)", async () => {
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    server.use(
      http.post("*/api/shares/research/10/activate", () =>
        HttpResponse.json({ detail: "Approve a note first" }, { status: 400 })),
    );
    const { user } = renderWithProviders(
      <ResearchLinks links={[link({ id: 10, spec_status: "draft", approved_count: 1 })]} reload={() => {}} />,
    );

    await user.click(screen.getByRole("button", { name: /Activate link/i }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("Approve a note first"));
  });
});

// ---------------------------------------------------------------------------- //
// (c) Manage modal — detail load, candidate approval, settings (TTL/scope), labs.
// ---------------------------------------------------------------------------- //
describe("ResearchLinks — Manage modal", () => {
  it("opens the modal and loads detail: approved notes + pending candidates", async () => {
    server.use(
      ...manageHandlers({
        approved: [{ id: 100, title: "Medications" }],
        candidates: [{ id: 200, title: "Allergies" }, { id: 201, title: "Family history" }],
      }),
    );
    const { user } = renderWithProviders(
      <ResearchLinks links={[link({ id: 1 })]} reload={() => {}} />,
    );

    await user.click(screen.getByRole("button", { name: /^Manage$/i }));

    // The eager researchDetail() resolves and the modal renders both lists.
    await screen.findByText("Medications");
    expect(screen.getByText("Allergies")).toBeInTheDocument();
    expect(screen.getByText("Approved — the link can read these (1)")).toBeInTheDocument();
    expect(screen.getByText("Pending candidates (2)")).toBeInTheDocument();
  });

  it("approves a candidate note — POSTs the note id to /approve", async () => {
    server.use(
      ...manageHandlers({ candidates: [{ id: 200, title: "Allergies" }] }),
      http.post("*/api/shares/research/1/approve", async ({ request }) => {
        posted.push({ url: request.url, method: "POST", body: await request.json() });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(
      <ResearchLinks links={[link({ id: 1 })]} reload={() => {}} />,
    );

    await user.click(screen.getByRole("button", { name: /^Manage$/i }));
    await screen.findByText("Allergies");

    await user.click(screen.getByRole("button", { name: /^Approve$/i }));

    await waitFor(() => expect(posted.length).toBe(1));
    expect(posted[0].url).toContain("/api/shares/research/1/approve");
    expect(posted[0].body).toEqual({ ids: [200] });
  });

  it("saves scope/persona/TTL settings — sends the edited fields + carried caps", async () => {
    let savedBody: any = null;
    server.use(
      ...manageHandlers({
        spec: {
          status: "active", persona_voice: "", topics: "", intro: "",
          bind: false, single_use: false, max_turns: 25, max_total_replies: 150,
        },
        expires_at: null,
      }),
      http.post("*/api/shares/research/1/details", async ({ request }) => {
        savedBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(
      <ResearchLinks links={[link({ id: 1 })]} reload={() => {}} />,
    );

    await user.click(screen.getByRole("button", { name: /^Manage$/i }));
    // Wait for settings to hydrate (the Save button appears once detail loads).
    const scope = await screen.findByPlaceholderText(/only medications and allergies/i);
    await user.type(scope, "only meds");

    // Set an expiry via ShareOptions (TTL in days).
    const ttl = screen.getByRole("spinbutton");
    await user.clear(ttl);
    await user.type(ttl, "30");

    await user.click(screen.getByRole("button", { name: /Save settings/i }));

    await waitFor(() => expect(savedBody).not.toBeNull());
    expect(savedBody.topics).toBe("only meds");
    expect(savedBody.ttl_days).toBe(30);
    // The caps are carried through so the details endpoint's defaults don't clobber them.
    expect(savedBody.max_turns).toBe(25);
    expect(savedBody.max_total_replies).toBe(150);
    // Confirmation state.
    await screen.findByRole("button", { name: /Saved/i });
  });

  it("attaches a lab analyte (scope) — POSTs the selected analyte + window to /labs", async () => {
    let labBody: any = null;
    server.use(
      ...manageHandlers(
        {},
        [{
          analyte: "ldl", test_name: "LDL Cholesterol", unit: "mg/dL",
          n: 4, first_at: "2025-01-01", last_at: "2026-01-01", last_value: "120", last_status: "high",
        }],
      ),
      http.post("*/api/shares/research/1/labs", async ({ request }) => {
        labBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(
      <ResearchLinks links={[link({ id: 1 })]} reload={() => {}} />,
    );

    await user.click(screen.getByRole("button", { name: /^Manage$/i }));
    // The analyte checkbox appears once getLabAnalytes() resolves.
    const checkbox = await screen.findByRole("checkbox", { name: /LDL Cholesterol/i });
    await user.click(checkbox);

    await user.click(screen.getByRole("button", { name: /Save labs/i }));

    await waitFor(() => expect(labBody).not.toBeNull());
    expect(labBody.analytes).toEqual(["ldl"]);
    expect(labBody.window_from).toBeNull();
    expect(labBody.window_to).toBeNull();
  });

  it("shows a loading placeholder, then a 'no candidates' empty state", async () => {
    server.use(...manageHandlers()); // empty approved + candidates
    const { user } = renderWithProviders(
      <ResearchLinks links={[link({ id: 1 })]} reload={() => {}} />,
    );

    await user.click(screen.getByRole("button", { name: /^Manage$/i }));
    await screen.findByText(/No new candidates/i);
    expect(screen.getByText(/Nothing approved yet/i)).toBeInTheDocument();
  });
});
