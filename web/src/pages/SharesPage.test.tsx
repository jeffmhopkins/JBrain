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
