// Component-integration (Tier 2) tests for OwnerChatPage — the OWNER side of an
// end-to-end-encrypted 1:1 share-chat. The owner opens a chat link they minted; the
// page fetches the chat detail, recovers the AES-256-GCM CHANNEL KEY from `owner_wrap`
// (sealed under a key derived from this device's access key — the server never sees it),
// and hands a real transport to <EncryptedChat>.
//
// The security invariants under test mirror EncryptedChat.test.tsx / SharesPage.test.tsx:
//   • the owner unwraps the SAME key that was sealed into owner_wrap (proved via the SAS),
//   • a sent message is sealed BEFORE the relay — only ciphertext (iv, ct) hits /send,
//   • an inbound relayed ciphertext decrypts client-side and renders as plaintext,
//   • End & save closes the relay then commits a PLAINTEXT transcript to the brain.
//
// Web Crypto: jsdom on Node 22 exposes a real globalThis.crypto.subtle, so we use the
// REAL crypto.ts (wrapKey / unwrapKey / encryptJSON / decryptJSON / sasFingerprint) for a
// genuine seal → relay → decrypt round-trip. Nothing in crypto.ts is stubbed.
//
// Relay SSE: openChatStream is a long-lived fetch+ReadableStream that's awkward to drive
// through MSW, so — exactly as EncryptedChat.test.tsx does — we vi.mock("../api") keeping
// every export REAL except openChatStream, which we swap for a scriptable fake. The page's
// own data calls (chatDetail / chatOwnerSend / chatOwnerClose / chatOwnerSave) stay real
// and go through MSW (onUnhandledRequest:"error" → every endpoint is mocked).
//
// Auth: the page reads useAuth() from ../App only for brainName, so — like the other page
// tests — we stub that module instead of dragging in the whole auth/router App tree.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { setAccessKey } from "../api";
import type { ChatStreamEvent } from "../api";
import {
  wrapKey, ownerPassword, decryptJSON, encryptJSON, sasFingerprint,
  generateChannelKey, type ChannelKey,
} from "../crypto";

vi.mock("../App", () => ({
  useAuth: () => ({ appTz: "UTC", brainName: "Test Brain" }),
  PWA_VERSION: "test",
}));

// --- openChatStream fake (the relay) ---------------------------------------- //
// Capture the onEvent callback so a test can emit relay events into the live page,
// and hold `done` open forever so the page's reconnect loop stays quiet.
let streamOnEvent: ((e: ChatStreamEvent) => void) | null = null;
const streamOpens: { path: string; auth: boolean }[] = [];

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    openChatStream: vi.fn(
      (path: string, auth: boolean, onEvent: (e: ChatStreamEvent) => void) => {
        streamOpens.push({ path, auth });
        streamOnEvent = onEvent;
        return { close: () => {}, done: new Promise<void>(() => {}) };
      },
    ),
  };
});

// Imported AFTER the mock factories (vi.mock is hoisted above imports).
import OwnerChatPage from "./OwnerChatPage";

function emit(e: ChatStreamEvent) {
  if (!streamOnEvent) throw new Error("stream not open yet");
  streamOnEvent(e);
}

const ACCESS_KEY = "owner-access-key";
const LINK_ID = 12;

// Every request the page makes, recorded so flows can assert routing + bodies.
let posted: { url: string; body: any }[] = [];
let channel: ChannelKey;

// A chat detail payload as the server returns it, carrying an owner_wrap sealed under
// THIS device's access key so the page's real unwrap path recovers `channel`.
async function detailPayload(over: Record<string, unknown> = {}) {
  const owner_wrap = await wrapKey(channel.raw, ownerPassword(ACCESS_KEY));
  return {
    link_id: LINK_ID, token: "tok12", url: "https://x/share/tok12",
    owner_wrap, persist: true, otp_required: false, status: "active",
    guest_name: "Sarah", owner_name: "Me", saved_note_slug: null, expires_at: null,
    present: { owner: true, guest: false },
    ...over,
  };
}

// Register the chat-detail GET the page issues on mount. Returns the resolved payload.
async function mountDetail(over: Record<string, unknown> = {}) {
  const d = await detailPayload(over);
  server.use(http.get(`*/api/shares/chat/${LINK_ID}`, () => HttpResponse.json(d)));
  return d;
}

function renderOwnerChat() {
  return renderWithProviders(
    <Routes><Route path="/shares/chat/:linkId" element={<OwnerChatPage />} /></Routes>,
    { route: `/shares/chat/${LINK_ID}` },
  );
}

beforeEach(async () => {
  posted = [];
  streamOnEvent = null;
  streamOpens.length = 0;
  channel = await generateChannelKey();
  setAccessKey(ACCESS_KEY);
  // cryptoAvailable() gates the E2E path on a secure context; jsdom reports false.
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
  // jsdom doesn't implement Element.scrollTo; the chat body scrolls on each message.
  if (!HTMLDivElement.prototype.scrollTo) HTMLDivElement.prototype.scrollTo = () => {};
});
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) Owner joins: detail fetched, key recovered from owner_wrap, surface renders.
// ---------------------------------------------------------------------------- //
describe("OwnerChatPage — join with the channel key from owner_wrap", () => {
  it("opens the secure channel and renders the chat surface for the recovered key", async () => {
    await mountDetail();
    renderOwnerChat();

    // The chat surface is up: placeholder names the peer (guest), and the safety check
    // shows. (Before unwrap succeeds the page only shows "Opening secure channel…".)
    await screen.findByPlaceholderText(/Message Sarah/i);
    // SAS is derived via async crypto.subtle, so await it (don't assert synchronously).
    await screen.findByText(/Safety check:/i);

    // PROOF it unwrapped the RIGHT key: the SAS emoji fingerprint shown matches the one
    // crypto.ts derives from the channel key's raw bytes (deterministic). A wrong key (or
    // a stubbed unwrap) could not produce this exact string.
    const expected = await sasFingerprint(channel.raw);
    expect(screen.getByText(expected)).toBeInTheDocument();

    // The owner relay stream was opened at after=0 with the authenticated flag.
    await waitFor(() => expect(streamOpens).toHaveLength(1));
    expect(streamOpens[0].path).toBe(`/api/shares/chat/${LINK_ID}/stream?after=0`);
    expect(streamOpens[0].auth).toBe(true);
  });
});

// ---------------------------------------------------------------------------- //
// (b) Sending encrypts BEFORE the relay — only ciphertext crosses /send.
// ---------------------------------------------------------------------------- //
describe("OwnerChatPage — send encrypts before the relay", () => {
  it("seals the message client-side: the /send body is opaque (iv, ct) ciphertext", async () => {
    await mountDetail();
    server.use(
      http.post(`*/api/shares/chat/${LINK_ID}/send`, async ({ request }) => {
        const body = await request.json();
        posted.push({ url: request.url, body });
        return HttpResponse.json({ ok: true, seq: 1 });
      }),
    );
    const { user } = renderOwnerChat();
    await screen.findByText(/Safety check:/i);

    const secret = "meet at the docks";
    await user.type(screen.getByPlaceholderText(/Message Sarah/i), secret);
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() => expect(posted).toHaveLength(1));
    const { url, body } = posted[0];
    expect(url).toContain(`/api/shares/chat/${LINK_ID}/send`);

    // INVARIANT: the relay payload is opaque — neither field holds the plaintext.
    expect(typeof body.iv).toBe("string");
    expect(typeof body.ct).toBe("string");
    expect(JSON.stringify(body)).not.toContain(secret);

    // And it's genuinely the message: decrypting with the channel key recovers it.
    const payload = await decryptJSON<{ t: string; text: string }>(channel.key, body.iv, body.ct);
    expect(payload).toEqual({ t: "text", text: secret });
  });
});

// ---------------------------------------------------------------------------- //
// (c) Receiving decrypts — a relayed ciphertext renders as the peer's plaintext.
// ---------------------------------------------------------------------------- //
describe("OwnerChatPage — receive decrypts inbound ciphertext", () => {
  it("decrypts a guest message relayed over the stream and shows the plaintext", async () => {
    await mountDetail();
    renderOwnerChat();
    await screen.findByText(/Safety check:/i);

    // The guest seals a message with the SAME channel key; the relay pushes only the
    // sealed bytes down the owner's stream.
    const sealed = await encryptJSON(channel.key, { t: "text", text: "hi from Sarah" });
    emit({ type: "message", seq: 1, sender: "guest", at: "", iv: sealed.iv, ct: sealed.ct });

    await screen.findByText("hi from Sarah");
  });

  it("silently skips an undecryptable (wrong-key / tampered) ciphertext", async () => {
    await mountDetail();
    renderOwnerChat();
    await screen.findByText(/Safety check:/i);

    // Sealed under a DIFFERENT key → GCM auth fails on decrypt, nothing renders.
    const stranger = await generateChannelKey();
    const sealed = await encryptJSON(stranger.key, { t: "text", text: "eavesdropper" });
    emit({ type: "message", seq: 1, sender: "guest", at: "", iv: sealed.iv, ct: sealed.ct });

    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("eavesdropper")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (c2) Presence — a relay presence event flips the peer indicator online.
// ---------------------------------------------------------------------------- //
describe("OwnerChatPage — peer presence from the relay", () => {
  it("shows the guest as away until a presence event marks them online", async () => {
    await mountDetail();
    renderOwnerChat();
    await screen.findByText(/Safety check:/i);

    // Owner is attached; the guest shows away until the relay says otherwise.
    expect(screen.getByText(/Sarah away/i)).toBeInTheDocument();
    emit({ type: "presence", owner: true, guest: true });
    await screen.findByText(/Sarah online/i);
  });
});

// ---------------------------------------------------------------------------- //
// (d) End & save — close the relay, then declassify the PLAINTEXT transcript.
// ---------------------------------------------------------------------------- //
describe("OwnerChatPage — End & save commits the decrypted transcript", () => {
  it("closes the relay then saves a plaintext, name-attributed transcript to the brain", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await mountDetail();
    let saveBody: any = null;
    server.use(
      http.post(`*/api/shares/chat/${LINK_ID}/close`, ({ request }) => {
        posted.push({ url: request.url, body: null });
        return HttpResponse.json({ ok: true });
      }),
      http.post(`*/api/shares/chat/${LINK_ID}/save`, async ({ request }) => {
        saveBody = await request.json();
        posted.push({ url: request.url, body: saveBody });
        return HttpResponse.json({ ok: true, note_slug: "chat-with-sarah", already_saved: false });
      }),
    );
    const { user } = renderOwnerChat();
    await screen.findByText(/Safety check:/i);

    // Two relayed messages (owner + guest), each sealed under the channel key.
    const a = await encryptJSON(channel.key, { t: "text", text: "hello" });
    const b = await encryptJSON(channel.key, { t: "text", text: "hi back" });
    emit({ type: "message", seq: 1, sender: "owner", at: "", iv: a.iv, ct: a.ct });
    emit({ type: "message", seq: 2, sender: "guest", at: "", iv: b.iv, ct: b.ct });
    await screen.findByText("hello");
    await screen.findByText("hi back");

    await user.click(screen.getByRole("button", { name: /End & save/i }));

    // The relay is closed first ...
    await waitFor(() => expect(posted.some((p) => p.url.includes(`/chat/${LINK_ID}/close`))).toBe(true));
    // ... then (after the settle timeout) the decrypted transcript is committed.
    await waitFor(() => expect(saveBody).not.toBeNull(), { timeout: 3000 });
    expect(saveBody.guest_name).toBe("Sarah");
    // The transcript is PLAINTEXT (decrypted client-side), attributed to display names.
    expect(saveBody.transcript_md).toContain("hello");
    expect(saveBody.transcript_md).toContain("hi back");
    expect(saveBody.transcript_md).toContain("Sarah");

    // The "Saved → note" banner links to the committed note.
    await screen.findByRole("link", { name: /Saved/i });
  });
});

// ---------------------------------------------------------------------------- //
// (e) Invalid / expired session — the page surfaces an error, no chat surface.
// ---------------------------------------------------------------------------- //
describe("OwnerChatPage — invalid / expired session", () => {
  it("shows the open-failure card (and a way back) when the detail fetch errors", async () => {
    server.use(
      http.get(`*/api/shares/chat/${LINK_ID}`, () =>
        HttpResponse.json({ detail: "Chat not found or expired" }, { status: 404 })),
    );
    renderOwnerChat();

    await screen.findByText(/Couldn't open chat/i);
    expect(screen.getByText("Chat not found or expired")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Back to Shares/i })).toBeInTheDocument();
    // No chat surface and no relay opened on the error path.
    expect(screen.queryByPlaceholderText(/Message/i)).not.toBeInTheDocument();
    expect(streamOpens).toHaveLength(0);
  });

  it("errors (no relay) when this device has no access key to derive the unwrap password", async () => {
    setAccessKey("");
    await mountDetail();
    renderOwnerChat();

    await screen.findByText(/No access key on this device/i);
    // Without a key the channel is never recovered, so no chat surface / stream.
    expect(screen.queryByPlaceholderText(/Message/i)).not.toBeInTheDocument();
    expect(streamOpens).toHaveLength(0);
  });
});
