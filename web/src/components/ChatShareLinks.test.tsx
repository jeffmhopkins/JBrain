// Component-integration (Tier 2) tests for <ChatShareLinks> — the OWNER's list of
// encrypted-chat share links plus the "New encrypted chat" creation form. This is the
// security-sensitive zero-knowledge surface: the AES-256 channel key is minted in THIS
// browser and only ever leaves as two WRAPPED copies (owner_wrap under the access key,
// guest_wrap under the link-fragment secret [+ OTP]). The recipient URL fragment (#s=…)
// is cached on-device for re-copy and is never on the wire.
//
// We render the component directly with `links` + a `reload` spy, since it's a leaf that
// takes both as props. Real WebCrypto runs (crypto.subtle in jsdom); only the network is
// mocked via MSW (onUnhandledRequest:"error"), so each test layers in just the chat
// endpoints its flow hits.
//
// Auth: the component reads useAuth() (appTz / brainName); stub ../App as the sibling
// SharesPage / ResearchLinks tests do. The on-device access key is seeded via setAccessKey
// and isSecureContext is forced true so cryptoAvailable() passes (jsdom reports false).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { setAccessKey } from "../api";
import { guestPassword, ownerPassword, unwrapKey } from "../crypto";

vi.mock("../App", () => ({
  useAuth: () => ({ appTz: "UTC", brainName: "Test Brain" }),
  PWA_VERSION: "test",
}));

// Imported AFTER the mock factory (vi.mock is hoisted above imports).
import ChatShareLinks, { type ChatLink } from "./ChatShareLinks";

function link(over: Partial<ChatLink> = {}): ChatLink {
  return {
    link_id: 1, persist: true, otp_required: false, pending_setup: false, status: "active",
    guest_name: null, owner_name: "Me", created_at: "2026-01-01 00:00:00", closed_at: null,
    last_guest_at: null, last_owner_at: null, url: "https://x/share/c1", token: "c1",
    label: null, expires_at: null, saved_note_slug: null, ...over,
  };
}

beforeEach(() => {
  setAccessKey("owner-access-key");
  // cryptoAvailable() also gates on a secure context; jsdom reports false by default.
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
});
afterEach(() => cleanup());

describe("ChatShareLinks — creation (zero-knowledge)", () => {
  it("mints the channel key client-side: the server only ever gets the two wraps", async () => {
    let createBody: any = null;
    server.use(
      http.post("*/api/shares/chat", async ({ request }) => {
        createBody = await request.json();
        return HttpResponse.json({ token: "chatok", link_id: 55, url: "https://x/share/chatok" });
      }),
    );
    const { user } = renderWithProviders(<ChatShareLinks links={[]} reload={() => {}} />);

    await user.click(screen.getByRole("button", { name: /New encrypted chat/i }));
    await user.click(screen.getByRole("button", { name: /Create chat link/i }));

    await waitFor(() => expect(createBody).not.toBeNull());
    const b = createBody;
    // INVARIANT: only the two wrapped copies cross the wire — no raw key field.
    expect(typeof b.owner_wrap).toBe("string");
    expect(typeof b.guest_wrap).toBe("string");
    expect(b.persist).toBe(true);
    expect(b.otp_required).toBe(false);

    // The reveal panel shows the link with the secret in the URL fragment (#s=…).
    const urlInput = (await screen.findByDisplayValue(/#s=/)) as HTMLInputElement;
    const secret = new URL(urlInput.value).hash.slice(3);
    expect(secret).toMatch(/^[A-Za-z0-9_-]+$/);
    // The raw secret is NEVER in the request body.
    expect(JSON.stringify(b)).not.toContain(secret);

    // ROUND-TRIP: both wraps decrypt to the SAME 32-byte (AES-256) key.
    const fromGuest = await unwrapKey(b.guest_wrap, guestPassword(secret));
    const fromOwner = await unwrapKey(b.owner_wrap, ownerPassword("owner-access-key"));
    expect(Array.from(fromGuest.raw)).toEqual(Array.from(fromOwner.raw));
    expect(fromGuest.raw.length).toBe(32);
  });

  it("with the one-time-code option: otp_required + a code mixed into guest_wrap", async () => {
    let createBody: any = null;
    server.use(
      http.post("*/api/shares/chat", async ({ request }) => {
        createBody = await request.json();
        return HttpResponse.json({ token: "chatok", link_id: 56, url: "https://x/share/chatok" });
      }),
    );
    const { user } = renderWithProviders(<ChatShareLinks links={[]} reload={() => {}} />);

    await user.click(screen.getByRole("button", { name: /New encrypted chat/i }));
    await user.click(screen.getByRole("checkbox", { name: /Require a one-time code/i }));
    await user.click(screen.getByRole("button", { name: /Create chat link/i }));

    await waitFor(() => expect(createBody).not.toBeNull());
    expect(createBody.otp_required).toBe(true);

    // The reveal panel shows a one-time code, delivered separately from the link.
    const codeEl = await screen.findByText(/^[A-Z2-9]{4}-[A-Z2-9]{4}$/);
    const otp = codeEl.textContent!.trim();
    const urlInput = (await screen.findByDisplayValue(/#s=/)) as HTMLInputElement;
    const secret = new URL(urlInput.value).hash.slice(3);

    // The OTP is out-of-band: never on the wire, and required to open the guest_wrap.
    expect(JSON.stringify(createBody)).not.toContain(otp);
    const owner = await unwrapKey(createBody.owner_wrap, ownerPassword("owner-access-key"));
    const withOtp = await unwrapKey(createBody.guest_wrap, guestPassword(secret, otp));
    expect(Array.from(withOtp.raw)).toEqual(Array.from(owner.raw));
    await expect(unwrapKey(createBody.guest_wrap, guestPassword(secret))).rejects.toBeTruthy();
  });

  it("toggling the form open then closed clears any prior result", async () => {
    const { user } = renderWithProviders(<ChatShareLinks links={[]} reload={() => {}} />);
    const toggle = screen.getByRole("button", { name: /New encrypted chat/i });
    await user.click(toggle);
    expect(screen.getByRole("button", { name: /Create chat link/i })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Close$/i }));
    expect(screen.queryByRole("button", { name: /Create chat link/i })).not.toBeInTheDocument();
  });
});

describe("ChatShareLinks — active list management", () => {
  it("renders an active chat with its badges and copies the cached recipient link", async () => {
    // Seed the on-device cached full URL (the #s= fragment is zero-knowledge server-side).
    localStorage.setItem("jbrain_chat_link_7", "https://x/share/c7#s=cachedsecret");
    const { user } = renderWithProviders(
      <ChatShareLinks
        links={[link({ link_id: 7, guest_name: "Sarah", label: "Chat with Sarah", persist: true })]}
        reload={() => {}}
      />,
    );
    expect(screen.getByText("Chat with Sarah")).toBeInTheDocument();
    expect(screen.getByText("history")).toBeInTheDocument(); // persist:true badge

    await user.click(screen.getByRole("button", { name: /Copy link/i }));
    await waitFor(async () =>
      expect(await navigator.clipboard.readText()).toBe("https://x/share/c7#s=cachedsecret"));
    expect(await screen.findByRole("button", { name: /Copied/i })).toBeInTheDocument();
  });

  it("ends a chat: confirms, POSTs /close, then reloads", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let closed = false;
    const reload = vi.fn();
    server.use(http.post("*/api/shares/chat/9/close", () => { closed = true; return HttpResponse.json({ ok: true }); }));
    const { user } = renderWithProviders(
      <ChatShareLinks links={[link({ link_id: 9 })]} reload={reload} />,
    );

    await user.click(screen.getByRole("button", { name: /^End$/i }));
    await waitFor(() => expect(closed).toBe(true));
    expect(reload).toHaveBeenCalled();
  });

  it("does NOT end the chat when the confirm is declined", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const reload = vi.fn();
    // No /close handler registered: if it fired, MSW's onUnhandledRequest:"error" would fail.
    const { user } = renderWithProviders(
      <ChatShareLinks links={[link({ link_id: 9 })]} reload={reload} />,
    );
    await user.click(screen.getByRole("button", { name: /^End$/i }));
    expect(reload).not.toHaveBeenCalled();
  });
});

describe("ChatShareLinks — draft (pending_setup) links", () => {
  it("finalizes a draft: mints the key, PATCHes /finalize, reveals the link", async () => {
    let finalizeBody: any = null;
    const reload = vi.fn();
    server.use(
      http.post("*/api/shares/chat/3/finalize", async ({ request }) => {
        finalizeBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(
      <ChatShareLinks
        links={[link({ link_id: 3, pending_setup: true, url: "https://x/share/c3", label: "Draft chat" })]}
        reload={reload}
      />,
    );
    expect(screen.getByText(/draft — finalize to get link/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Finalize/i }));
    await waitFor(() => expect(finalizeBody).not.toBeNull());
    // Finalize sends the same two wraps as create.
    expect(typeof finalizeBody.owner_wrap).toBe("string");
    expect(typeof finalizeBody.guest_wrap).toBe("string");
    // The reveal panel opens with the recipient URL fragment.
    expect(await screen.findByDisplayValue(/https:\/\/x\/share\/c3#s=/)).toBeInTheDocument();
    expect(reload).toHaveBeenCalled();
  });

  it("discards a draft: confirms, POSTs /revoke, reloads", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let revoked = false;
    const reload = vi.fn();
    server.use(http.post("*/api/shares/3/revoke", () => { revoked = true; return HttpResponse.json({ ok: true }); }));
    const { user } = renderWithProviders(
      <ChatShareLinks links={[link({ link_id: 3, pending_setup: true })]} reload={reload} />,
    );
    await user.click(screen.getByRole("button", { name: /^Delete$/i }));
    await waitFor(() => expect(revoked).toBe(true));
    expect(reload).toHaveBeenCalled();
  });
});

describe("ChatShareLinks — edge cases", () => {
  it("renders nothing in the active list when there are no links (empty state)", () => {
    renderWithProviders(<ChatShareLinks links={[]} reload={() => {}} />);
    // The header + toggle always render; no chat cards do.
    expect(screen.getByText("Encrypted chats")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^End$/i })).not.toBeInTheDocument();
  });

  it("filters out ended chats — only active ones are listed", () => {
    renderWithProviders(
      <ChatShareLinks
        links={[
          link({ link_id: 1, label: "Active one", status: "active" }),
          link({ link_id: 2, label: "Ended one", status: "closed" }),
        ]}
        reload={() => {}}
      />,
    );
    expect(screen.getByText("Active one")).toBeInTheDocument();
    expect(screen.queryByText("Ended one")).not.toBeInTheDocument();
  });

  it("alerts (and does not call the server) when creating without an access key", async () => {
    setAccessKey("");
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    // No create handler: a stray POST would trip onUnhandledRequest:"error".
    const { user } = renderWithProviders(<ChatShareLinks links={[]} reload={() => {}} />);
    await user.click(screen.getByRole("button", { name: /New encrypted chat/i }));
    await user.click(screen.getByRole("button", { name: /Create chat link/i }));
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("No access key on this device."));
  });
});
