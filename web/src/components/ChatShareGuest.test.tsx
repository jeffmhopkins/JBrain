// Component-integration (Tier 2) tests for ChatShareGuest — the GUEST (recipient)
// side of an end-to-end-encrypted 1:1 share-chat. The recipient opens a /share link;
// the AES-256-GCM CHANNEL KEY is recovered ENTIRELY in this browser from the URL
// fragment secret (#s=…) [+ an out-of-band one-time code] — neither ever reaches the
// server. The component then hands a real guest transport to <EncryptedChat>.
//
// The security invariants under test mirror OwnerChatPage.test.tsx / EncryptedChat.test.tsx:
//   • the guest unwraps the SAME key that was sealed into guest_wrap (proved via the SAS),
//   • when OTP is required, a wrong / missing code CANNOT unwrap the key (GCM auth fails)
//     and no chat surface / relay stream appears,
//   • a sent message is sealed BEFORE the relay — only ciphertext (iv, ct) hits /chat/send,
//   • an inbound relayed ciphertext decrypts client-side and renders as the peer's plaintext,
//   • invalid / expired / locked sessions surface a friendly error and never open a stream.
//
// Web Crypto: jsdom on Node 22 exposes a real globalThis.crypto.subtle, so we use the
// REAL crypto.ts (wrapKey / guestPassword / unwrapKey / encryptJSON / decryptJSON /
// sasFingerprint / newFragmentSecret) for a genuine seal → relay → decrypt round-trip.
// Nothing in crypto.ts is stubbed.
//
// Relay SSE: openChatStream is a long-lived fetch+ReadableStream that's awkward to drive
// through MSW, so — exactly as OwnerChatPage.test.tsx does — we vi.mock("../api") keeping
// every export REAL except openChatStream, which we swap for a scriptable fake. The
// component's own public data calls (chatJoin / chatGuestSend) stay real and go through
// MSW (onUnhandledRequest:"error" → every endpoint hit is mocked).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import type { ChatStreamEvent } from "../api";
import {
  wrapKey, guestPassword, decryptJSON, encryptJSON, sasFingerprint,
  generateChannelKey, newFragmentSecret, type ChannelKey,
} from "../crypto";

// --- openChatStream fake (the relay) ---------------------------------------- //
// Capture the onEvent callback so a test can emit relay events into the live page,
// and hold `done` open forever so the component's reconnect loop stays quiet.
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

// Imported AFTER the mock factory (vi.mock is hoisted above imports).
import ChatShareGuest from "./ChatShareGuest";

function emit(e: ChatStreamEvent) {
  if (!streamOnEvent) throw new Error("stream not open yet");
  streamOnEvent(e);
}

const TOKEN = "tok-guest";
const OTP = "ABCD-2345";

// Every public POST the component makes, recorded so flows can assert routing + bodies.
let posted: { url: string; body: any }[] = [];
let channel: ChannelKey;
let secret: string;

interface ChatLanding {
  brain_name: string; owner_name?: string | null; otp_required?: boolean; persist?: boolean;
  requires_claim?: boolean; guest_wrap?: string; guest_name?: string | null;
}

// Seal the channel key into a guest_wrap recoverable from `secret` [+ otp], exactly as
// the owner's browser would have when minting the link — so the component's real unwrap
// path recovers `channel`.
async function makeWrap(otp?: string) {
  return wrapKey(channel.raw, guestPassword(secret, otp));
}

function renderGuest(landing: Partial<ChatLanding> = {}) {
  const full: ChatLanding = {
    brain_name: "Test Brain", owner_name: "Owner", otp_required: false,
    persist: true, requires_claim: false, guest_name: "Sarah", ...landing,
  };
  return renderWithProviders(<ChatShareGuest token={TOKEN} landing={full} />);
}

beforeEach(async () => {
  posted = [];
  streamOnEvent = null;
  streamOpens.length = 0;
  localStorage.clear();
  channel = await generateChannelKey();
  secret = newFragmentSecret();
  // The component reads the fragment secret from location.hash (#s=…).
  window.location.hash = `#s=${secret}`;
  // cryptoAvailable() gates the E2E path on a secure context; jsdom reports false.
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
  // jsdom doesn't implement Element.scrollTo; the chat body scrolls on each message.
  if (!HTMLDivElement.prototype.scrollTo) HTMLDivElement.prototype.scrollTo = () => {};
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

// ---------------------------------------------------------------------------- //
// (a) Guest joins (no OTP): key recovered from guest_wrap in the landing, surface up.
// ---------------------------------------------------------------------------- //
describe("ChatShareGuest — join with the channel key from guest_wrap", () => {
  it("unwraps the key from the fragment secret and renders the chat surface", async () => {
    const guest_wrap = await makeWrap();
    const { user } = renderGuest({ guest_wrap, owner_name: "Owner" });

    // The landing card invites the recipient; no chat surface until they join.
    expect(screen.getByText(/invited to a private chat/i)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Message/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Join securely/i }));

    // The chat surface is up: composer names the peer (the owner), SAS shows.
    await screen.findByPlaceholderText(/Message Owner/i);
    await screen.findByText(/Safety check:/i);

    // PROOF it unwrapped the RIGHT key: the emoji SAS shown matches the one crypto.ts
    // derives from the channel key's raw bytes (deterministic). A wrong key (or a stubbed
    // unwrap) could not reproduce this exact string.
    const expected = await sasFingerprint(channel.raw);
    expect(screen.getByText(expected)).toBeInTheDocument();

    // The guest relay stream was opened at after=0 with the UNauthenticated flag (public).
    await waitFor(() => expect(streamOpens).toHaveLength(1));
    expect(streamOpens[0].path).toBe(`/api/share/${TOKEN}/chat/stream?after=0`);
    expect(streamOpens[0].auth).toBe(false);
  });

  it("claims the link first (chatJoin) when the landing requires a name", async () => {
    // requires_claim → the guest_wrap is fetched from /chat/join (not in the landing).
    const guest_wrap = await makeWrap();
    server.use(
      http.post(`*/api/share/${TOKEN}/chat/join`, async ({ request }) => {
        posted.push({ url: request.url, body: await request.json() });
        return HttpResponse.json({ guest_wrap, persist: true, guest_name: "Sarah" });
      }),
    );
    const { user } = renderGuest({ requires_claim: true, guest_wrap: undefined, guest_name: "" });

    await user.type(screen.getByPlaceholderText(/Your name/i), "Sarah");
    await user.click(screen.getByRole("button", { name: /Join securely/i }));

    // The join POST carried the typed name; the surface comes up with the recovered key.
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0].url).toContain(`/api/share/${TOKEN}/chat/join`);
    expect(posted[0].body).toEqual({ name: "Sarah" });
    await screen.findByText(/Safety check:/i);
    expect(screen.getByText(await sasFingerprint(channel.raw))).toBeInTheDocument();
    // The name is remembered for next time.
    expect(localStorage.getItem("jbrain_share_name")).toBe("Sarah");
  });
});

// ---------------------------------------------------------------------------- //
// (b) The OTP gate — the right code unwraps; a wrong / missing one cannot.
// ---------------------------------------------------------------------------- //
describe("ChatShareGuest — OTP gate", () => {
  it("unwraps only with the correct one-time code", async () => {
    // guest_wrap is sealed under secret + OTP, so the code is required to recover the key.
    const guest_wrap = await makeWrap(OTP);
    const { user } = renderGuest({ otp_required: true, guest_wrap });

    await user.type(screen.getByPlaceholderText(/One-time code/i), OTP);
    await user.click(screen.getByRole("button", { name: /Join securely/i }));

    await screen.findByText(/Safety check:/i);
    expect(screen.getByText(await sasFingerprint(channel.raw))).toBeInTheDocument();
  });

  it("refuses a WRONG code: GCM auth fails, no surface, no relay", async () => {
    const guest_wrap = await makeWrap(OTP);
    const { user } = renderGuest({ otp_required: true, guest_wrap });

    await user.type(screen.getByPlaceholderText(/One-time code/i), "ZZZZ-9999");
    await user.click(screen.getByRole("button", { name: /Join securely/i }));

    // unwrapKey throws (wrong password) → friendly OTP error, never a chat surface.
    await screen.findByText(/That code didn't work/i);
    expect(screen.queryByPlaceholderText(/Message/i)).not.toBeInTheDocument();
    expect(streamOpens).toHaveLength(0);
  });

  it("blocks joining with a MISSING code before any unwrap is attempted", async () => {
    const guest_wrap = await makeWrap(OTP);
    const { user } = renderGuest({ otp_required: true, guest_wrap });

    await user.click(screen.getByRole("button", { name: /Join securely/i }));

    await screen.findByText(/Enter the one-time code/i);
    expect(screen.queryByPlaceholderText(/Message/i)).not.toBeInTheDocument();
    expect(streamOpens).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------- //
// (c) Sending encrypts BEFORE the relay — only ciphertext crosses /chat/send.
// ---------------------------------------------------------------------------- //
describe("ChatShareGuest — send encrypts before the relay", () => {
  it("seals the message client-side: the /chat/send body is opaque (iv, ct) ciphertext", async () => {
    const guest_wrap = await makeWrap();
    server.use(
      http.post(`*/api/share/${TOKEN}/chat/send`, async ({ request }) => {
        posted.push({ url: request.url, body: await request.json() });
        return HttpResponse.json({ ok: true, seq: 1 });
      }),
    );
    const { user } = renderGuest({ guest_wrap });
    await user.click(screen.getByRole("button", { name: /Join securely/i }));
    await screen.findByText(/Safety check:/i);

    const secretMsg = "see you at the docks";
    await user.type(screen.getByPlaceholderText(/Message Owner/i), secretMsg);
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() => expect(posted).toHaveLength(1));
    const { url, body } = posted[0];
    expect(url).toContain(`/api/share/${TOKEN}/chat/send`);

    // INVARIANT: the relay payload is opaque — neither field holds the plaintext.
    expect(typeof body.iv).toBe("string");
    expect(typeof body.ct).toBe("string");
    expect(JSON.stringify(body)).not.toContain(secretMsg);

    // And it's genuinely the message: decrypting with the channel key recovers it.
    const payload = await decryptJSON<{ t: string; text: string }>(channel.key, body.iv, body.ct);
    expect(payload).toEqual({ t: "text", text: secretMsg });
  });
});

// ---------------------------------------------------------------------------- //
// (d) Receiving decrypts — a relayed ciphertext renders as the owner's plaintext.
// ---------------------------------------------------------------------------- //
describe("ChatShareGuest — receive decrypts inbound ciphertext", () => {
  it("decrypts an owner message relayed over the stream and shows the plaintext", async () => {
    const guest_wrap = await makeWrap();
    const { user } = renderGuest({ guest_wrap });
    await user.click(screen.getByRole("button", { name: /Join securely/i }));
    await screen.findByText(/Safety check:/i);

    // The owner seals a message with the SAME channel key; the relay pushes only the
    // sealed bytes down the guest's stream.
    const sealed = await encryptJSON(channel.key, { t: "text", text: "hi from Owner" });
    emit({ type: "message", seq: 1, sender: "owner", at: "", iv: sealed.iv, ct: sealed.ct });

    await screen.findByText("hi from Owner");
  });

  it("silently skips an undecryptable (wrong-key / tampered) ciphertext", async () => {
    const guest_wrap = await makeWrap();
    const { user } = renderGuest({ guest_wrap });
    await user.click(screen.getByRole("button", { name: /Join securely/i }));
    await screen.findByText(/Safety check:/i);

    // Sealed under a DIFFERENT key → GCM auth fails on decrypt, nothing renders.
    const stranger = await generateChannelKey();
    const sealed = await encryptJSON(stranger.key, { t: "text", text: "eavesdropper" });
    emit({ type: "message", seq: 1, sender: "owner", at: "", iv: sealed.iv, ct: sealed.ct });

    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("eavesdropper")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (e) SAS / safety check — both sides derive the SAME fingerprint from the key.
// ---------------------------------------------------------------------------- //
describe("ChatShareGuest — safety check (SAS)", () => {
  it("renders the SAS string the OWNER derives from the same channel key", async () => {
    // The "owner" is whoever sealed guest_wrap from `channel`; the guest recovers that
    // exact key, so the emoji fingerprint must match byte-for-byte on both ends.
    const guest_wrap = await makeWrap();
    const { user } = renderGuest({ guest_wrap });
    await user.click(screen.getByRole("button", { name: /Join securely/i }));

    const expected = await sasFingerprint(channel.raw);
    await screen.findByText(expected);
    expect(screen.getByText(/Safety check:/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (f) Invalid / incomplete / locked — friendly error, no surface, no relay.
// ---------------------------------------------------------------------------- //
describe("ChatShareGuest — invalid / incomplete / locked links", () => {
  it("errors when the link fragment secret is missing (incomplete link)", async () => {
    window.location.hash = "";   // no #s=… → the secret can't be read
    const guest_wrap = await makeWrap();
    const { user } = renderGuest({ guest_wrap });

    await user.click(screen.getByRole("button", { name: /Join securely/i }));

    await screen.findByText(/link is incomplete/i);
    expect(screen.queryByPlaceholderText(/Message/i)).not.toBeInTheDocument();
    expect(streamOpens).toHaveLength(0);
  });

  it("errors with a tampered / wrong-secret guest_wrap (GCM auth fails, no OTP)", async () => {
    // guest_wrap sealed under a DIFFERENT secret than the one in the URL fragment.
    const otherSecret = newFragmentSecret();
    const guest_wrap = await wrapKey(channel.raw, guestPassword(otherSecret));
    const { user } = renderGuest({ guest_wrap });

    await user.click(screen.getByRole("button", { name: /Join securely/i }));

    await screen.findByText(/Couldn't open this chat/i);
    expect(screen.queryByPlaceholderText(/Message/i)).not.toBeInTheDocument();
    expect(streamOpens).toHaveLength(0);
  });

  it("surfaces a locked link (chatJoin 403) as 'already open in another browser'", async () => {
    server.use(
      http.post(`*/api/share/${TOKEN}/chat/join`, () =>
        HttpResponse.json({ detail: "locked" }, { status: 403 })),
    );
    const { user } = renderGuest({ requires_claim: true, guest_wrap: undefined, guest_name: "" });

    await user.type(screen.getByPlaceholderText(/Your name/i), "Sarah");
    await user.click(screen.getByRole("button", { name: /Join securely/i }));

    await screen.findByText(/already open in another browser/i);
    expect(screen.queryByPlaceholderText(/Message/i)).not.toBeInTheDocument();
    expect(streamOpens).toHaveLength(0);
  });
});
