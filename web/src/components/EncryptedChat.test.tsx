// Component-integration (Tier 2) tests for the end-to-end-encrypted 1:1 chat
// (EncryptedChat.tsx). These exercise the security-critical invariant that the
// component only ever puts CIPHERTEXT on the wire and only ever renders text it
// decrypted itself with the channel key.
//
// Web Crypto: jsdom on Node 22 exposes a real globalThis.crypto.subtle, so we use
// the REAL crypto.ts API (generateChannelKey / encryptJSON / decryptJSON) for a
// genuine encrypt -> relay -> decrypt round-trip. Nothing is stubbed there.
//
// Relay SSE: openChatStream is a long-lived fetch+ReadableStream that's awkward to
// drive through MSW, so — exactly as Chat.test.tsx does for streamChat — we
// vi.mock("../api") keeping every export real EXCEPT openChatStream, which we swap
// for a scriptable fake. A per-test `emit` lets a test push relay events (presence
// / message / closed) into the live component on demand. The transport object the
// component is handed is a plain fake we build per test; its `send` records the
// sealed bytes so we can assert the server never sees plaintext, then echoes the
// message back through `emit` to model the relay fan-out.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import type { ChatStreamEvent } from "../api";
import {
  generateChannelKey, decryptJSON, encryptJSON, sasFingerprint, type ChannelKey,
} from "../crypto";
import type { ChatTransport } from "./EncryptedChat";

// --- openChatStream fake ---------------------------------------------------- //
// The component opens a stream and gets back { close, done }. We capture the
// onEvent callback so the active test can emit relay events into it, and hold
// `done` open (never resolve it) so the component's reconnect loop stays quiet.
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
import EncryptedChat from "./EncryptedChat";

function emit(e: ChatStreamEvent) {
  if (!streamOnEvent) throw new Error("stream not open yet");
  streamOnEvent(e);
}

// A relay fake: records every sealed (iv, ct) the component sends, never seeing
// plaintext. `seq` auto-increments like the real server. Files are kept in-memory.
interface SentMsg { iv: string; ct: string }
function makeTransport(overrides: Partial<ChatTransport> = {}) {
  const sent: SentMsg[] = [];
  const files: Record<number, { iv: string; buf: ArrayBuffer }> = {};
  let seq = 0;
  let nextFileId = 100;
  const transport: ChatTransport = {
    auth: true,
    streamPath: (after) => `/stream?after=${after}`,
    send: async (iv, ct) => { sent.push({ iv, ct }); return { seq: ++seq }; },
    uploadFile: async (iv, blob) => {
      const id = nextFileId++;
      files[id] = { iv, buf: await blob.arrayBuffer() };
      return id;
    },
    fetchFile: async (fileId) => files[fileId],
    ...overrides,
  };
  return { transport, sent, files };
}

let channel: ChannelKey;

beforeEach(async () => {
  streamOnEvent = null;
  streamOpens.length = 0;
  channel = await generateChannelKey();
  // jsdom doesn't implement Element.scrollTo; the chat body scrolls on each message.
  if (!HTMLDivElement.prototype.scrollTo) {
    HTMLDivElement.prototype.scrollTo = () => {};
  }
});
afterEach(() => cleanup());

function renderChat(props: Partial<React.ComponentProps<typeof EncryptedChat>> = {}) {
  const { transport, sent, files } = makeTransport(props.transport ? {} : {});
  const result = renderWithProviders(
    <EncryptedChat
      channel={channel}
      role="owner"
      meName="Me"
      peerName="Sarah"
      brandName="JBrain"
      persist={true}
      transport={props.transport || transport}
      {...props}
    />,
  );
  return { ...result, transport: props.transport || transport, sent, files };
}

// ---------------------------------------------------------------------------- //
// (a) Channel-key SAS fingerprint — both sides derive the SAME emoji string.
// ---------------------------------------------------------------------------- //
describe("EncryptedChat — safety fingerprint (SAS)", () => {
  it("shows the SAS emoji string derived from the channel key", async () => {
    renderChat();
    // The component renders sasFingerprint(channel.raw); assert it matches the
    // value the crypto module derives from the same key bytes (deterministic).
    const expected = await sasFingerprint(channel.raw);
    await screen.findByText(expected);
    expect(screen.getByText(/Safety check:/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) Sending encrypts BEFORE the relay — the server only ever sees ciphertext.
// ---------------------------------------------------------------------------- //
describe("EncryptedChat — send encrypts before the relay", () => {
  it("seals the message so the plaintext never appears in the sent (iv, ct)", async () => {
    const { user, transport, sent } = renderChat();
    await screen.findByText(/Safety check:/i);   // stream + SAS settled

    const box = screen.getByPlaceholderText(/Message Sarah/i);
    const secret = "attack at dawn";
    await user.type(box, secret);
    await user.click(screen.getByRole("button", { name: /^Send$/i }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const { iv, ct } = sent[0];
    // INVARIANT: the relay payload is opaque — neither field contains the plaintext.
    expect(ct).not.toContain(secret);
    expect(iv).not.toContain(secret);
    expect(JSON.stringify(sent)).not.toContain(secret);

    // And it's genuinely the message: decrypting with the channel key recovers it.
    const payload = await decryptJSON<{ t: string; text: string }>(channel.key, iv, ct);
    expect(payload).toEqual({ t: "text", text: secret });

    // The composer clears on a successful send.
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(""));
    void transport;
  });
});

// ---------------------------------------------------------------------------- //
// (c) Receiving decrypts — a relayed ciphertext renders as plaintext.
// ---------------------------------------------------------------------------- //
describe("EncryptedChat — receive decrypts inbound ciphertext", () => {
  it("decrypts a relayed message and renders the peer's plaintext bubble", async () => {
    renderChat();
    await screen.findByText(/Safety check:/i);

    // The peer (guest) seals a message with the SAME channel key and the relay
    // pushes only the sealed bytes down the stream.
    const sealed = await encryptJSON(channel.key, { t: "text", text: "hi from Sarah" });
    emit({ type: "message", seq: 1, sender: "guest", at: "", iv: sealed.iv, ct: sealed.ct });

    // The component decrypts it client-side and shows the plaintext.
    await screen.findByText("hi from Sarah");
  });

  it("full round-trip: a sent message echoed back by the relay decrypts identically", async () => {
    const { user, sent } = renderChat();
    await screen.findByText(/Safety check:/i);

    await user.type(screen.getByPlaceholderText(/Message Sarah/i), "round trip");
    await user.click(screen.getByRole("button", { name: /^Send$/i }));
    await waitFor(() => expect(sent).toHaveLength(1));

    // Model the relay fan-out: the same sealed bytes come back on the stream.
    emit({ type: "message", seq: 1, sender: "owner", at: "", iv: sent[0].iv, ct: sent[0].ct });
    await screen.findByText("round trip");   // plaintext in == plaintext out
  });

  it("silently skips an undecryptable (wrong-key/tampered) ciphertext", async () => {
    renderChat();
    await screen.findByText(/Safety check:/i);

    // A message sealed under a DIFFERENT channel key: GCM auth fails on decrypt.
    const stranger = await generateChannelKey();
    const sealed = await encryptJSON(stranger.key, { t: "text", text: "eavesdropper" });
    emit({ type: "message", seq: 1, sender: "guest", at: "", iv: sealed.iv, ct: sealed.ct });

    // Give the async decrypt a chance to reject, then confirm nothing rendered.
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("eavesdropper")).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (d) Presence + closed lifecycle from relay events.
// ---------------------------------------------------------------------------- //
describe("EncryptedChat — presence and close from the relay", () => {
  it("reflects peer presence and reports it upward", async () => {
    const onPresence = vi.fn();
    renderChat({ onPresence });
    await screen.findByText(/Safety check:/i);

    // Owner is viewing; peer (guest) shows away until the relay says otherwise.
    expect(screen.getByText(/Sarah away/i)).toBeInTheDocument();
    emit({ type: "presence", owner: true, guest: true });

    await screen.findByText(/Sarah online/i);
    await waitFor(() => expect(onPresence).toHaveBeenCalledWith({ owner: true, guest: true }));
  });

  it("locks the composer and shows the ended note on a close event", async () => {
    const onClosed = vi.fn();
    renderChat({ onClosed });
    await screen.findByText(/Safety check:/i);
    expect(screen.getByPlaceholderText(/Message Sarah/i)).toBeInTheDocument();

    emit({ type: "closed" });

    await screen.findByText(/This chat has ended\./i);
    expect(screen.queryByPlaceholderText(/Message Sarah/i)).not.toBeInTheDocument();
    expect(onClosed).toHaveBeenCalled();
  });

  it("shows the ephemeral notice when persist is off and there are no messages", async () => {
    renderChat({ persist: false });
    await screen.findByText(/ephemeral chat — nothing is kept on the server/i);
  });
});

// ---------------------------------------------------------------------------- //
// (e) Owner save (getSaveData) assembles a plaintext transcript client-side.
// ---------------------------------------------------------------------------- //
describe("EncryptedChat — owner save assembles decrypted transcript", () => {
  it("builds a markdown transcript with attributed names from decrypted messages", async () => {
    const ref = { current: null as null | { getSaveData: () => Promise<any> } };
    renderChat({ ref: ref as any, meName: "Owner", peerName: "Sarah" });
    await screen.findByText(/Safety check:/i);

    // Two sealed messages arrive (owner + guest), each under the channel key.
    const a = await encryptJSON(channel.key, { t: "text", text: "hello" });
    const b = await encryptJSON(channel.key, { t: "text", text: "hi back" });
    emit({ type: "message", seq: 1, sender: "owner", at: "", iv: a.iv, ct: a.ct });
    emit({ type: "message", seq: 2, sender: "guest", at: "", iv: b.iv, ct: b.ct });
    await screen.findByText("hello");
    await screen.findByText("hi back");

    const data = await ref.current!.getSaveData();
    // The transcript is plaintext (decrypted here), attributed to the display names.
    expect(data.transcript_md).toContain("**Owner:** hello");
    expect(data.transcript_md).toContain("**Sarah:** hi back");
    expect(data.attachments).toEqual([]);
  });
});

// ---------------------------------------------------------------------------- //
// (f) Stream wiring: opens the correct path/auth for the owner transport.
// ---------------------------------------------------------------------------- //
describe("EncryptedChat — stream wiring", () => {
  it("opens the relay stream at after=0 with the transport's auth flag", async () => {
    renderChat();
    await waitFor(() => expect(streamOpens).toHaveLength(1));
    expect(streamOpens[0].path).toBe("/stream?after=0");
    expect(streamOpens[0].auth).toBe(true);
  });
});
