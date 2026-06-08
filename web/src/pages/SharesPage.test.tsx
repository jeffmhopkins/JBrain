// Component-integration (Tier 2) tests for the Shares page — focused on the
// SECURITY-SENSITIVE encrypted share-chat surface it owns through <ChatShareLinks>,
// plus the ordinary note-share link management it renders directly.
//
// The crypto invariant under test: when the owner mints an encrypted-chat link the
// AES-256-GCM CHANNEL KEY is generated in the browser and leaves it only as two
// WRAPPED copies (owner_wrap / guest_wrap). The raw key and the link-fragment secret
// must NEVER hit the server — the fragment secret lives only in the `#s=…` of the URL
// shown to the owner. We assert this against the real /api/shares/chat create body and
// prove the guest_wrap round-trips back to a usable key with the fragment secret.
//
// Web Crypto: jsdom on Node 22 exposes a real globalThis.crypto.subtle, so the page's
// real key-wrap path runs unmodified and we verify the round-trip with crypto.ts.
//
// Network: every endpoint the page hits is mocked via MSW (onUnhandledRequest:"error").
// The page reads useAuth() from ../App only for appTz/brainName, so — exactly as the
// other page tests do (NotePage/CalendarPage) — we stub that module to avoid dragging
// in the whole auth/router App tree.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { setAccessKey } from "../api";
import { guestPassword, ownerPassword, unwrapKey } from "../crypto";

// Stub the auth context the page + ChatShareLinks read (appTz for timestamps,
// brainName as the default owner display name).
vi.mock("../App", () => ({
  useAuth: () => ({ appTz: "UTC", brainName: "Test Brain" }),
  PWA_VERSION: "test",
}));

// Imported AFTER the mock factory (vi.mock is hoisted above imports).
import SharesPage from "./SharesPage";

// Records every request the page makes so flows can assert routing + bodies without
// reaching into component internals.
let posted: { url: string; method: string; body: any }[] = [];

// The default /api/shares payload. Tests override the slices they care about.
function sharesPayload(over: Record<string, unknown> = {}) {
  return {
    links: [],
    proposals: [],
    history: [],
    guided_links: [],
    guided_pending: [],
    guided_ended: [],
    guided_history: [],
    research_links: [],
    lab_links: [],
    chat_links: [],
    ...over,
  };
}

// Baseline handlers: the page loads /api/shares on mount; nothing else fires unless a
// flow triggers it. Each test adds the flow-specific handlers it needs.
function sharesHandlers(payload: Record<string, unknown> = {}) {
  return [
    http.get("*/api/shares", () => HttpResponse.json(sharesPayload(payload))),
  ];
}

beforeEach(() => {
  posted = [];
  // ChatShareLinks.create() refuses without an access key on the device; seed one.
  setAccessKey("owner-access-key");
  // cryptoAvailable() also gates create() on a secure context; jsdom reports false.
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
  // NB: navigator.clipboard is provided by userEvent.setup() (installed in
  // renderWithProviders); the copy flow reads the written value back through it.
});
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) Page load + ordinary note-share link management (list / revoke).
// ---------------------------------------------------------------------------- //
describe("SharesPage — note-share link management", () => {
  it("lists an active note-share link with its url and badges", async () => {
    server.use(...sharesHandlers({
      links: [{
        id: 7, token: "tok7", scope: "view", label: "for Pat", created_at: "2026-01-01 00:00:00",
        last_used_at: null, expires_at: null, bind: 0, bound_at: null, pending: 0,
        note_title: "notes/Trip Plan", note_slug: "trip-plan", url: "https://x/share/tok7",
      }],
    }));
    renderWithProviders(<SharesPage />);

    // The leaf title, the scope badge, the label and the link url all render.
    await screen.findByText("Trip Plan");
    expect(screen.getByText("view")).toBeInTheDocument();
    expect(screen.getByText("for Pat")).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://x/share/tok7")).toBeInTheDocument();
  });

  it("revokes a link — fires the revoke POST and drops the row optimistically", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      ...sharesHandlers({
        links: [{
          id: 9, token: "tok9", scope: "edit", label: null, created_at: "2026-01-01 00:00:00",
          last_used_at: null, expires_at: null, bind: 0, bound_at: null, pending: 0,
          note_title: "notes/Secret", note_slug: "secret", url: "https://x/share/tok9",
        }],
      }),
      http.post("*/api/shares/9/revoke", async ({ request }) => {
        posted.push({ url: request.url, method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Secret");

    await user.click(screen.getByRole("button", { name: /^Revoke$/i }));

    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/9/revoke"))).toBe(true));
    // Optimistic removal: the row is gone immediately.
    await waitFor(() => expect(screen.queryByText("Secret")).not.toBeInTheDocument());
  });

  it("copies a link url to the clipboard", async () => {
    server.use(...sharesHandlers({
      links: [{
        id: 3, token: "t3", scope: "view", label: null, created_at: "2026-01-01 00:00:00",
        last_used_at: null, expires_at: null, bind: 0, bound_at: null, pending: 0,
        note_title: "notes/Doc", note_slug: "doc", url: "https://x/share/t3",
      }],
    }));
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Doc");

    // The note-share row's Copy button (the first one on the page). user-event
    // installs its own clipboard stub, so read the written value back through it.
    await user.click(screen.getAllByRole("button", { name: /^Copy$/i })[0]);
    await waitFor(async () =>
      expect(await navigator.clipboard.readText()).toBe("https://x/share/t3"));
  });

  it("shows the empty-state hint when there are no active note links", async () => {
    server.use(...sharesHandlers());
    renderWithProviders(<SharesPage />);
    await screen.findByText(/No links yet/i);
  });
});

// ---------------------------------------------------------------------------- //
// (b) Create an encrypted chat link — the crypto invariants.
// ---------------------------------------------------------------------------- //
describe("SharesPage — create encrypted chat link (zero-knowledge)", () => {
  // Open the "New encrypted chat" form and submit it, capturing the create body.
  async function createChat(
    user: ReturnType<typeof renderWithProviders>["user"],
    opts: { persist?: boolean; otp?: boolean } = {},
  ) {
    let createBody: any = null;
    server.use(
      http.post("*/api/shares/chat", async ({ request }) => {
        createBody = await request.json();
        return HttpResponse.json({ token: "chatok", link_id: 55, url: "https://x/share/chatok" });
      }),
    );
    await user.click(await screen.findByRole("button", { name: /New encrypted chat/i }));

    if (opts.persist === false) {
      await user.click(screen.getByRole("checkbox", { name: /Keep history/i }));
    }
    if (opts.otp) {
      await user.click(screen.getByRole("checkbox", { name: /Require a one-time code/i }));
    }
    await user.click(screen.getByRole("button", { name: /Create chat link/i }));

    await waitFor(() => expect(createBody).not.toBeNull());
    return () => createBody;
  }

  it("generates the channel key client-side: the server only ever gets the two wraps", async () => {
    server.use(...sharesHandlers());
    const { user } = renderWithProviders(<SharesPage />);
    const body = await createChat(user);
    const b = body();

    // INVARIANT: the create request carries only the two WRAPPED copies of the key —
    // owner_wrap (sealed under the access key) and guest_wrap (sealed under the link
    // fragment secret). There is no raw key field on the wire.
    expect(typeof b.owner_wrap).toBe("string");
    expect(typeof b.guest_wrap).toBe("string");
    expect(b.persist).toBe(true);
    expect(b.otp_required).toBe(false);

    // The reveal panel shows the link with the secret in the URL fragment (#s=…).
    const urlInput = (await screen.findByDisplayValue(/#s=/)) as HTMLInputElement;
    const fragment = new URL(urlInput.value).hash; // "#s=<secret>"
    expect(fragment).toMatch(/^#s=[A-Za-z0-9_-]+$/);
    const secret = fragment.slice(3);

    // INVARIANT: the raw channel key and the fragment secret are NEVER in the request.
    // The wraps are opaque ciphertext; the secret only exists in the URL fragment.
    expect(JSON.stringify(b)).not.toContain(secret);

    // ROUND-TRIP: the guest_wrap really does unwrap with the fragment secret, and the
    // recovered key is the SAME one sealed into owner_wrap (both decrypt to one key).
    const fromGuest = await unwrapKey(b.guest_wrap, guestPassword(secret));
    const fromOwner = await unwrapKey(b.owner_wrap, ownerPassword("owner-access-key"));
    expect(Array.from(fromGuest.raw)).toEqual(Array.from(fromOwner.raw));
    expect(fromGuest.raw.length).toBe(32); // AES-256
  });

  it("honors the ephemeral (history off) choice — persist:false on the wire", async () => {
    server.use(...sharesHandlers());
    const { user } = renderWithProviders(<SharesPage />);
    const body = await createChat(user, { persist: false });
    expect(body().persist).toBe(false);
  });

  it("with the one-time code option: otp_required + a code mixed into guest_wrap", async () => {
    server.use(...sharesHandlers());
    const { user } = renderWithProviders(<SharesPage />);
    const body = await createChat(user, { otp: true });
    const b = body();
    expect(b.otp_required).toBe(true);

    // The reveal panel shows a one-time code, delivered SEPARATELY from the link.
    const codeEl = await screen.findByText(/^[A-Z2-9]{4}-[A-Z2-9]{4}$/);
    const otp = codeEl.textContent!.trim();

    const urlInput = (await screen.findByDisplayValue(/#s=/)) as HTMLInputElement;
    const secret = new URL(urlInput.value).hash.slice(3);

    // The OTP must NOT be on the wire (it's out-of-band) ...
    expect(JSON.stringify(b)).not.toContain(otp);
    // ... and the guest_wrap only opens when the secret is salted with that OTP:
    // the fragment secret ALONE (a leaked link) can't decrypt.
    const owner = await unwrapKey(b.owner_wrap, ownerPassword("owner-access-key"));
    const withOtp = await unwrapKey(b.guest_wrap, guestPassword(secret, otp));
    expect(Array.from(withOtp.raw)).toEqual(Array.from(owner.raw));
    await expect(unwrapKey(b.guest_wrap, guestPassword(secret))).rejects.toBeTruthy();
  });
});

// ---------------------------------------------------------------------------- //
// (c) Listing existing encrypted chats + ending one (relay-management).
// ---------------------------------------------------------------------------- //
describe("SharesPage — encrypted chat list management", () => {
  it("renders an active encrypted chat and ends it via the close endpoint", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      ...sharesHandlers({
        chat_links: [{
          link_id: 12, persist: true, otp_required: false, pending_setup: false, status: "active",
          guest_name: "Sarah", owner_name: "Me", created_at: "2026-01-01 00:00:00", closed_at: null,
          last_guest_at: null, last_owner_at: null, url: "https://x/share/c12", token: "c12",
          label: "Chat with Sarah", expires_at: null, saved_note_slug: null,
        }],
      }),
      http.post("*/api/shares/chat/12/close", async ({ request }) => {
        posted.push({ url: request.url, method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Chat with Sarah");
    // history-vs-ephemeral badge reflects persist:true.
    expect(screen.getByText("history")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^End$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/chat/12/close"))).toBe(true));
  });
});

// ---------------------------------------------------------------------------- //
// (d) Research links — the read-only scoped-Q&A surface rendered via <ResearchLinks>.
//     Listing live + draft links, activate, revoke, copy, and the Manage modal
//     (approve/dismiss candidates, attach labs, save settings, view sessions).
// ---------------------------------------------------------------------------- //
const RLINK = (over: Record<string, unknown> = {}) => ({
  id: 30, label: "Doctor Q&A", url: "https://x/share/r30", spec_status: "active",
  approved_count: 2, lab_count: 0, sessions: 0, reply_count: 1, max_total_replies: 20, ...over,
});

describe("SharesPage — research links", () => {
  it("lists a live research link with badges + url; copies it", async () => {
    server.use(...sharesHandlers({ research_links: [RLINK()] }));
    const { user } = renderWithProviders(<SharesPage />);

    await screen.findByText("Doctor Q&A");
    expect(screen.getByText("live")).toBeInTheDocument();
    expect(screen.getByText("2 notes")).toBeInTheDocument();
    expect(screen.getByText("1/20 answers used")).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://x/share/r30")).toBeInTheDocument();

    // ResearchLinks.copy() uses navigator.clipboard (user-event stub) directly.
    await user.click(screen.getByRole("button", { name: /^Copy$/i }));
    await waitFor(async () => expect(await navigator.clipboard.readText()).toBe("https://x/share/r30"));
  });

  it("revokes a live research link via the shared revoke POST", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      ...sharesHandlers({ research_links: [RLINK({ id: 31 })] }),
      http.post("*/api/shares/31/revoke", () => {
        posted.push({ url: "/api/shares/31/revoke", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Doctor Q&A");

    await user.click(screen.getByRole("button", { name: /^Revoke$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/31/revoke"))).toBe(true));
  });

  it("activates a draft research link (research/:id/activate)", async () => {
    server.use(
      ...sharesHandlers({ research_links: [RLINK({ id: 32, spec_status: "draft", sessions: 0 })] }),
      http.post("*/api/shares/research/32/activate", () => {
        posted.push({ url: "/api/shares/research/32/activate", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Doctor Q&A");
    // Draft shows "draft — not live" and an Activate (not a url field).
    expect(screen.getByText("draft — not live")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Activate link$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/research/32/activate"))).toBe(true));
  });

  it("disables Activate on a draft with nothing approved", async () => {
    server.use(...sharesHandlers({
      research_links: [RLINK({ id: 33, spec_status: "draft", approved_count: 0, lab_count: 0 })],
    }));
    renderWithProviders(<SharesPage />);
    await screen.findByText("Doctor Q&A");
    expect(screen.getByRole("button", { name: /^Activate link$/i })).toBeDisabled();
  });

  it("opens Manage: approves a candidate and removes an approved note", async () => {
    let approveBody: any = null;
    let removeBody: any = null;
    server.use(
      ...sharesHandlers({ research_links: [RLINK({ id: 40 })] }),
      http.get("*/api/shares/research/40", () => HttpResponse.json({
        spec: { status: "active", persona_voice: "", topics: "", intro: "", bind: 0, single_use: 0, max_turns: 10, max_total_replies: 20 },
        approved: [{ id: 101, title: "notes/Meds" }],
        candidates: [{ id: 202, title: "notes/Allergies" }],
        labs: { analytes: [], window_from: null, window_to: null },
        sessions: [], expires_at: null, bound: false,
      })),
      http.get("*/api/medical/labs/analytes", () => HttpResponse.json({ analytes: [] })),
      http.post("*/api/shares/research/40/approve", async ({ request }) => {
        approveBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
      http.post("*/api/shares/research/40/remove", async ({ request }) => {
        removeBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Doctor Q&A");

    await user.click(screen.getByRole("button", { name: /^Manage$/i }));
    // Modal loads detail: approved note + candidate appear.
    await screen.findByText("notes/Meds");
    expect(screen.getByText("notes/Allergies")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Approve$/i }));
    await waitFor(() => expect(approveBody).toEqual({ ids: [202] }));

    await user.click(screen.getByRole("button", { name: /^Remove$/i }));
    await waitFor(() => expect(removeBody).toEqual({ ids: [101] }));
  });

  it("Manage: saves settings carrying the existing caps", async () => {
    let detailsBody: any = null;
    server.use(
      ...sharesHandlers({ research_links: [RLINK({ id: 41 })] }),
      http.get("*/api/shares/research/41", () => HttpResponse.json({
        spec: { status: "active", persona_voice: "warm", topics: "meds", intro: "hi", bind: 0, single_use: 0, max_turns: 7, max_total_replies: 15 },
        approved: [{ id: 1, title: "notes/A" }], candidates: [],
        labs: { analytes: [], window_from: null, window_to: null },
        sessions: [], expires_at: null, bound: false,
      })),
      http.get("*/api/medical/labs/analytes", () => HttpResponse.json({ analytes: [] })),
      http.post("*/api/shares/research/41/details", async ({ request }) => {
        detailsBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Doctor Q&A");
    await user.click(screen.getByRole("button", { name: /^Manage$/i }));
    await screen.findByText("notes/A");

    await user.click(screen.getByRole("button", { name: /^Save settings$/i }));
    await waitFor(() => expect(detailsBody).not.toBeNull());
    // The caps from the loaded spec must be re-sent so the endpoint doesn't reset them.
    expect(detailsBody.max_turns).toBe(7);
    expect(detailsBody.max_total_replies).toBe(15);
    expect(detailsBody.topics).toBe("meds");
  });

  it("Manage: attaches a lab analyte (research/:id/labs)", async () => {
    let labBody: any = null;
    server.use(
      ...sharesHandlers({ research_links: [RLINK({ id: 42 })] }),
      http.get("*/api/shares/research/42", () => HttpResponse.json({
        spec: { status: "active", persona_voice: "", topics: "", intro: "", bind: 0, single_use: 0, max_turns: 10, max_total_replies: 20 },
        approved: [], candidates: [],
        labs: { analytes: [], window_from: null, window_to: null },
        sessions: [], expires_at: null, bound: false,
      })),
      http.get("*/api/medical/labs/analytes", () => HttpResponse.json({
        analytes: [{ analyte: "glucose", test_name: "Glucose", last_value: "95", unit: "mg/dL" }],
      })),
      http.post("*/api/shares/research/42/labs", async ({ request }) => {
        labBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Doctor Q&A");
    await user.click(screen.getByRole("button", { name: /^Manage$/i }));

    // The lab picker lists the available analyte; select it then save. The modal
    // also renders the bind/single-use checkboxes, so target this one by its label.
    await screen.findByText("Glucose");
    await user.click(screen.getByRole("checkbox", { name: /Glucose/i }));
    await user.click(screen.getByRole("button", { name: /^Save labs$/i }));
    await waitFor(() => expect(labBody).not.toBeNull());
    expect(labBody.analytes).toEqual(["glucose"]);
  });

  it("Manage: lists a session and loads its transcript on View", async () => {
    server.use(
      ...sharesHandlers({ research_links: [RLINK({ id: 43, sessions: 1 })] }),
      http.get("*/api/shares/research/43", () => HttpResponse.json({
        spec: { status: "active", persona_voice: "", topics: "", intro: "", bind: 0, single_use: 0, max_turns: 10, max_total_replies: 20 },
        approved: [], candidates: [],
        labs: { analytes: [], window_from: null, window_to: null },
        sessions: [{ id: 9, name: "Reviewer", turn_count: 2, retrieved: 3, denied_count: 0 }],
        expires_at: null, bound: false,
      })),
      http.get("*/api/medical/labs/analytes", () => HttpResponse.json({ analytes: [] })),
      http.get("*/api/shares/research/43/sessions/9", () => HttpResponse.json({
        transcript: [{ role: "user", content: "what meds?" }, { role: "assistant", content: "ibuprofen" }],
      })),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Doctor Q&A");
    await user.click(screen.getByRole("button", { name: /^Manage$/i }));
    await screen.findByText(/Reviewer/);

    await user.click(screen.getByRole("button", { name: /^View$/i }));
    await screen.findByText("ibuprofen");
  });

  it("shows the create-via-assistant hint even with zero research links", async () => {
    server.use(...sharesHandlers());
    renderWithProviders(<SharesPage />);
    await screen.findByText(/scoped to notes you approve/i);
  });
});

// ---------------------------------------------------------------------------- //
// (e) Lab-share links — the PHI audit surface rendered via <LabShareLinks>.
//     The section only renders when there are links; list / copy / activity-audit / revoke.
// ---------------------------------------------------------------------------- //
const LABLINK = (over: Record<string, unknown> = {}) => ({
  id: 50, label: "Cholesterol panel", url: "https://x/share/l50", created_at: "2026-01-01 00:00:00",
  expires_at: "2026-12-31 00:00:00", allow_chat: 1, analyte_count: 3, sessions: 2,
  reply_count: 0, max_total_replies: 0, ...over,
});

describe("SharesPage — lab-share links", () => {
  it("does NOT render the lab section when there are no lab links", async () => {
    server.use(...sharesHandlers());
    renderWithProviders(<SharesPage />);
    await screen.findByText(/No links yet/i); // page settled
    expect(screen.queryByText("Lab result links")).not.toBeInTheDocument();
  });

  it("lists a lab link with its badges + expiry and copies the url", async () => {
    server.use(...sharesHandlers({ lab_links: [LABLINK()] }));
    const { user } = renderWithProviders(<SharesPage />);

    await screen.findByText("Cholesterol panel");
    expect(screen.getByText("Lab result links")).toBeInTheDocument();
    expect(screen.getByText("3 results")).toBeInTheDocument();
    expect(screen.getByText("AI chat")).toBeInTheDocument();
    expect(screen.getByText("2 views")).toBeInTheDocument();
    expect(screen.getByText(/expires 2026-12-31/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Copy$/i }));
    await waitFor(async () => expect(await navigator.clipboard.readText()).toBe("https://x/share/l50"));
  });

  it("opens the activity audit and renders a session row", async () => {
    server.use(
      ...sharesHandlers({ lab_links: [LABLINK({ id: 51 })] }),
      http.get("*/api/shares/labs/51/audit", () => HttpResponse.json({
        sessions: [{
          id: 1, name: "Dr. Lin", status: "active", turns: 4, charted: ["ldl", "hdl"],
          denied: 0, created_at: "2026-02-01T10:00:00", last_at: "2026-02-01T10:30:00",
        }],
      })),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Cholesterol panel");

    await user.click(screen.getByRole("button", { name: /^Activity$/i }));
    await screen.findByText("Dr. Lin");
    expect(screen.getByText(/2 charts/)).toBeInTheDocument();
    expect(screen.getByText(/4 questions/)).toBeInTheDocument();
  });

  it("shows the empty-audit message when no one has opened the link", async () => {
    server.use(
      ...sharesHandlers({ lab_links: [LABLINK({ id: 52 })] }),
      http.get("*/api/shares/labs/52/audit", () => HttpResponse.json({ sessions: [] })),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Cholesterol panel");

    await user.click(screen.getByRole("button", { name: /^Activity$/i }));
    await screen.findByText(/No one has opened this link yet/i);
  });

  it("revokes a lab link via the shared revoke POST", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      ...sharesHandlers({ lab_links: [LABLINK({ id: 53 })] }),
      http.post("*/api/shares/53/revoke", () => {
        posted.push({ url: "/api/shares/53/revoke", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Cholesterol panel");

    await user.click(screen.getByRole("button", { name: /^Revoke$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/53/revoke"))).toBe(true));
  });
});

// ---------------------------------------------------------------------------- //
// (f) Guided intake links + the inline pending/ended review surfaces and lazy
//     per-link session list (all rendered directly by SharesPage).
// ---------------------------------------------------------------------------- //
const GLINK = (over: Record<string, unknown> = {}) => ({
  id: 60, token: "g60", url: "https://x/share/g60", goal: "Onboarding interview", intro: "hello",
  sub_prompt: "ask about goals", spec_status: "active", bind: 0, single_use: 0, started: 0,
  note_title: "notes/Intake", note_slug: "intake", submitted: 0, expires_at: null, ...over,
});

describe("SharesPage — guided intake links", () => {
  it("lists a live guided link with its url + Revoke", async () => {
    server.use(...sharesHandlers({ guided_links: [GLINK()] }));
    renderWithProviders(<SharesPage />);

    await screen.findByText("Onboarding interview");
    expect(screen.getByText("live")).toBeInTheDocument();
    expect(screen.getByText("Guided intake links")).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://x/share/g60")).toBeInTheDocument();
  });

  it("activates a draft guided link (guided activate)", async () => {
    server.use(
      ...sharesHandlers({ guided_links: [GLINK({ id: 61, spec_status: "draft" })] }),
      http.post("*/api/shares/guided/61/activate", () => {
        posted.push({ url: "/api/shares/guided/61/activate", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");
    expect(screen.getByText("draft — not live")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Activate link \(approval #1\)/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/guided/61/activate"))).toBe(true));
  });

  it("lazily loads the per-link conversations list when expanded", async () => {
    server.use(
      ...sharesHandlers({ guided_links: [GLINK({ id: 62, started: 1 })] }),
      http.get("*/api/shares/guided/62/sessions", () => HttpResponse.json({
        sessions: [{
          id: 5, name: "Casey", status: "submitted", end_reason: null, turn_count: 3,
          created_at: "2026-01-01 00:00:00", completed_at: "2026-01-02 00:00:00",
          has_document: 1, has_transcript: 1,
        }],
      })),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    await user.click(screen.getByRole("button", { name: /View conversations \(1\)/i }));
    await screen.findByText("Casey");
    expect(screen.getByText("completed")).toBeInTheDocument();
  });

  it("renders the empty-sessions note when the link has starts but the list is empty", async () => {
    server.use(
      ...sharesHandlers({ guided_links: [GLINK({ id: 63, started: 1 })] }),
      http.get("*/api/shares/guided/63/sessions", () => HttpResponse.json({ sessions: [] })),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    await user.click(screen.getByRole("button", { name: /View conversations \(1\)/i }));
    await screen.findByText(/No one has started this intake yet/i);
  });

  it("approves a pending intake (guided accept) and renders its drafted doc", async () => {
    server.use(
      ...sharesHandlers({
        guided_pending: [{
          id: 70, name: "Jordan", document_md: "# Summary\n\nrelevant facts", goal: "Health intake",
          note_title: "notes/Health", note_slug: "health", completed_at: "2026-01-03 00:00:00",
          transcript: [{ role: "assistant", content: "how are you?" }, { role: "user", content: "fine" }],
        }],
      }),
      http.post("*/api/shares/guided/sessions/70/accept", () => {
        posted.push({ url: "/api/shares/guided/sessions/70/accept", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText(/Health intake/);
    expect(screen.getByText("Intakes to review (approval #2)")).toBeInTheDocument();
    expect(screen.getByText("Summary")).toBeInTheDocument();

    // Expand the conversation, then approve.
    await user.click(screen.getByRole("button", { name: /View conversation \(2\)/i }));
    await screen.findByText("fine");

    await user.click(screen.getByRole("button", { name: /Approve & save/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/guided/sessions/70/accept"))).toBe(true));
  });

  it("re-opens an auto-ended (abuse) guided chat via guided reopen", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      ...sharesHandlers({
        guided_ended: [{
          id: 80, name: "Sam", end_reason: "ended:abuse", goal: "Survey", note_title: "notes/Survey",
          note_slug: "survey", link_id: 80, link_status: "revoked", completed_at: "2026-01-04 00:00:00",
          transcript: [{ role: "user", content: "rude thing" }],
        }],
      }),
      http.post("*/api/shares/guided/sessions/80/reopen", () => {
        posted.push({ url: "/api/shares/guided/sessions/80/reopen", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Survey");
    expect(screen.getByText("Auto-ended chats")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Re-open link/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/guided/sessions/80/reopen"))).toBe(true));
  });
});

// ---------------------------------------------------------------------------- //
// (g) Pending edit proposals + the History roll-up (both rendered inline).
// ---------------------------------------------------------------------------- //
describe("SharesPage — proposals + history", () => {
  it("renders a pending proposal and accepts it (proposals/:id/accept)", async () => {
    server.use(
      ...sharesHandlers({
        proposals: [{
          id: 90, note_title: "notes/Recipe", note_slug: "recipe",
          proposed_content: "new text", current_content: "old text",
          proposer_name: "Alex", proposer_note: "fixed a typo", created_at: "2026-01-01 00:00:00", stale: false,
        }],
      }),
      http.post("*/api/shares/proposals/90/accept", () => {
        posted.push({ url: "/api/shares/proposals/90/accept", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Recipe");
    expect(screen.getByText("Pending proposals")).toBeInTheDocument();
    expect(screen.getByText("Alex proposed")).toBeInTheDocument();
    expect(screen.getByText(/fixed a typo/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Accept$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/proposals/90/accept"))).toBe(true));
  });

  it("rejects a proposal optimistically (proposals/:id/reject)", async () => {
    const proposal = {
      id: 91, note_title: "notes/Doc", note_slug: "doc",
      proposed_content: "b", current_content: "a",
      proposer_name: null, proposer_note: null, created_at: "2026-01-01 00:00:00", stale: true,
    };
    // The page reloads /api/shares after rejecting; mirror the server by dropping the
    // proposal once the reject POST lands, so the row stays gone after the reload.
    let rejected = false;
    server.use(
      http.get("*/api/shares", () =>
        HttpResponse.json(sharesPayload({ proposals: rejected ? [] : [proposal] }))),
      http.post("*/api/shares/proposals/91/reject", () => {
        rejected = true;
        posted.push({ url: "/api/shares/proposals/91/reject", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Doc");
    // "stale" badge surfaces when the note changed since the proposal.
    expect(screen.getByText("stale")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Reject$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/proposals/91/reject"))).toBe(true));
    await waitFor(() => expect(screen.queryByText("Doc")).not.toBeInTheDocument());
  });

  it("renders the History roll-up across edit + guided + ended-chat sources", async () => {
    server.use(...sharesHandlers({
      history: [{
        id: 1, proposer_name: "Pat", status: "accepted", created_at: "2026-01-01 00:00:00",
        resolved_at: "2026-01-02 00:00:00", note_title: "notes/Edited", note_slug: "edited",
      }],
      guided_history: [{
        id: 2, name: "Lee", goal: "Past intake", disposition: "saved",
        note_title: "notes/IntakeDone", note_slug: "intake-done", completed_at: "2026-01-03 00:00:00",
      }],
      chat_links: [{
        link_id: 3, persist: true, otp_required: false, pending_setup: false, status: "closed",
        guest_name: "Robin", owner_name: "Me", created_at: "2026-01-01 00:00:00", closed_at: "2026-01-05 00:00:00",
        last_guest_at: null, last_owner_at: null, url: "https://x/share/c3", token: "c3",
        label: "Past chat", expires_at: null, saved_note_slug: null,
      }],
    }));
    renderWithProviders(<SharesPage />);

    await screen.findByText("History");
    expect(screen.getByText("Edited")).toBeInTheDocument();
    expect(screen.getByText("Past intake")).toBeInTheDocument();
    expect(screen.getByText("Past chat")).toBeInTheDocument();
    expect(screen.getByText(/edit · Pat/)).toBeInTheDocument();
    expect(screen.getByText(/guided · Lee/)).toBeInTheDocument();
  });
});
