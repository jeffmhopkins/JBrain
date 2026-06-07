import { forwardRef, ReactNode, useEffect, useImperativeHandle, useRef, useState } from "react";
import { ChannelKey, decryptBytes, decryptJSON, encryptBytes, encryptJSON, sasFingerprint } from "../crypto";
import { ChatStreamEvent, openChatStream } from "../api";
import { Icon } from "../components/Icon";

// The transport is whatever moves sealed bytes for this side (owner vs recipient). The
// component is identical for both — it only ever sees ciphertext on the wire.
export interface ChatTransport {
  auth: boolean;
  streamPath: (after: number) => string;
  send: (iv: string, ct: string) => Promise<{ seq: number }>;
  uploadFile: (iv: string, blob: Blob) => Promise<number>;
  fetchFile: (fileId: number) => Promise<{ iv: string; buf: ArrayBuffer }>;
}

interface FileRef { file_id: number; name: string; mime: string; size: number; }
interface Msg { seq: number; sender: "owner" | "guest"; at: string; text?: string; file?: FileRef; }
// The encrypted message payload shape (never seen by the server).
type Payload = { t: "text"; text: string } | { t: "file"; file: FileRef };

const MAX_FILE = 100 * 1024 * 1024;

export interface SaveData { transcript_md: string; attachments: { name: string; mime: string; data: string }[]; }
export interface ChatHandle { getSaveData: () => Promise<SaveData>; }

function bufToB64(buf: ArrayBuffer): string {
  const b = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return btoa(s);
}

const EncryptedChat = forwardRef<ChatHandle, {
  channel: ChannelKey;
  role: "owner" | "guest";
  meName: string;
  peerName: string;
  transport: ChatTransport;
  brandName: string;
  persist: boolean;
  initialClosed?: boolean;
  headerExtra?: ReactNode;
  onPresence?: (p: { owner: boolean; guest: boolean }) => void;
  onClosed?: () => void;
}>(function EncryptedChat(
  { channel, role, meName, peerName, transport, brandName, persist, initialClosed, headerExtra, onPresence, onClosed }, ref,
) {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [presence, setPresence] = useState({ owner: role === "owner", guest: role === "guest" });
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [closed, setClosed] = useState(!!initialClosed);
  const [sas, setSas] = useState("");
  const [urls, setUrls] = useState<Record<number, string>>({});      // decrypted object URLs by file_id

  const lastSeq = useRef(0);
  const closedRef = useRef(!!initialClosed);
  const seen = useRef<Set<number>>(new Set());
  const scroller = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => { sasFingerprint(channel.raw).then(setSas); }, [channel]);

  // Live stream with auto-reconnect (resume from the last seq so nothing is missed).
  useEffect(() => {
    let handle: { close: () => void; done: Promise<void> } | null = null;
    let stopped = false;
    async function onEvent(e: ChatStreamEvent) {
      if (e.type === "presence") {
        const p = { owner: !!e.owner, guest: !!e.guest };
        setPresence(p); onPresence?.(p);
      } else if (e.type === "closed") {
        closedRef.current = true; setClosed(true); onClosed?.();
      } else if (e.type === "message" && e.seq && !seen.current.has(e.seq)) {
        seen.current.add(e.seq);
        if (e.seq > lastSeq.current) lastSeq.current = e.seq;
        try {
          const p = await decryptJSON<Payload>(channel.key, e.iv!, e.ct!);
          const m: Msg = { seq: e.seq, sender: e.sender!, at: e.at || "", ...(p.t === "text" ? { text: p.text } : { file: p.file }) };
          setMessages((ms) => [...ms, m].sort((a, b) => a.seq - b.seq));
          if (p.t === "file" && p.file.mime.startsWith("image/")) loadFile(p.file);   // inline image preview
        } catch { /* undecryptable (shouldn't happen) — skip */ }
      }
    }
    function loop() {
      if (stopped || closedRef.current) return;
      handle = openChatStream(transport.streamPath(lastSeq.current), transport.auth, onEvent);
      handle.done.then(() => { if (!stopped && !closedRef.current) setTimeout(loop, 1500); });
    }
    loop();
    return () => { stopped = true; handle?.close(); };
  }, [channel, transport]);

  useEffect(() => { scroller.current?.scrollTo(0, scroller.current.scrollHeight); }, [messages, presence]);

  async function loadFile(f: FileRef): Promise<string | null> {
    if (urls[f.file_id]) return urls[f.file_id];
    try {
      const { iv, buf } = await transport.fetchFile(f.file_id);
      const plain = await decryptBytes(channel.key, iv, buf);
      const url = URL.createObjectURL(new Blob([plain], { type: f.mime }));
      setUrls((u) => ({ ...u, [f.file_id]: url }));
      return url;
    } catch { return null; }
  }

  async function sendText() {
    const t = text.trim();
    if (!t || busy || closed) return;
    setBusy(true);
    try {
      const sealed = await encryptJSON(channel.key, { t: "text", text: t } as Payload);
      await transport.send(sealed.iv, sealed.ct);
      setText("");
    } catch { alert("Couldn't send."); }
    finally { setBusy(false); }
  }

  async function sendFile(file: File) {
    if (file.size > MAX_FILE) { alert("File too large (100 MB max)."); return; }
    setBusy(true);
    try {
      const { iv, blob } = await encryptBytes(channel.key, await file.arrayBuffer());
      const fileId = await transport.uploadFile(iv, blob);
      const ref: FileRef = { file_id: fileId, name: file.name, mime: file.type || "application/octet-stream", size: file.size };
      const sealed = await encryptJSON(channel.key, { t: "file", file: ref } as Payload);
      await transport.send(sealed.iv, sealed.ct);
    } catch { alert("Couldn't send the file."); }
    finally { setBusy(false); if (fileInput.current) fileInput.current.value = ""; }
  }

  async function openFile(f: FileRef) {
    const url = await loadFile(f);
    if (url) window.open(url, "_blank");
  }

  // Owner save: assemble a plaintext transcript + decrypt every attachment, all client-side.
  useImperativeHandle(ref, () => ({
    async getSaveData(): Promise<SaveData> {
      const lines: string[] = [];
      const attachments: { name: string; mime: string; data: string }[] = [];
      for (const m of messages) {
        const who = m.sender === "owner" ? meName : peerName;
        if (m.text) {
          lines.push(`**${who}:** ${m.text}`);
        } else if (m.file) {
          lines.push(`**${who}:** _sent a file — ${m.file.name}_`);
          try {
            const { iv, buf } = await transport.fetchFile(m.file.file_id);
            const plain = await decryptBytes(channel.key, iv, buf);
            attachments.push({ name: m.file.name, mime: m.file.mime, data: bufToB64(plain) });
          } catch { /* skip a file we can't fetch */ }
        }
      }
      return { transcript_md: lines.join("\n\n"), attachments };
    },
  }), [messages, meName, peerName, channel, transport]);

  const peerHere = role === "owner" ? presence.guest : presence.owner;

  return (
    <div className="echat">
      <div className="echat-head">
        <span className="brand">{brandName}<span className="dot">.</span></span>
        <span className="badge echat-lock" title="End-to-end encrypted">🔒 end-to-end encrypted</span>
        <span className="spacer" />
        <span className={"echat-presence " + (peerHere ? "on" : "")}>
          <span className="echat-dot" /> {peerName} {peerHere ? "online" : "away"}
        </span>
        {headerExtra}
      </div>
      {sas && (
        <div className="echat-sas" title="Both sides should see the same emojis — confirm out loud to rule out a man-in-the-middle.">
          Safety check: <strong>{sas}</strong>
        </div>
      )}

      <div className="echat-body" ref={scroller}>
        {!persist && messages.length === 0 && (
          <div className="echat-note">This is an ephemeral chat — nothing is kept on the server.</div>
        )}
        {messages.map((m) => {
          const mine = m.sender === role;
          return (
            <div key={m.seq} className={"echat-msg " + (mine ? "me" : "them")}>
              {m.text && <div className="echat-bubble">{m.text}</div>}
              {m.file && (
                <div className="echat-bubble echat-file">
                  {m.file.mime.startsWith("image/") && urls[m.file.file_id]
                    ? <img src={urls[m.file.file_id]} alt={m.file.name} onClick={() => openFile(m.file!)} />
                    : <button className="ghost" onClick={() => openFile(m.file!)}>
                        <Icon name="clip" size={13} /> {m.file.name}
                      </button>}
                </div>
              )}
            </div>
          );
        })}
        {closed && <div className="echat-note">This chat has ended.</div>}
      </div>

      {!closed && (
        <div className="echat-composer">
          <button className="icon-btn" title="Attach a file" disabled={busy} onClick={() => fileInput.current?.click()}>
            <Icon name="clip" size={18} />
          </button>
          <input ref={fileInput} type="file" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) sendFile(f); }} />
          <textarea value={text} placeholder={`Message ${peerName}…`} rows={1} disabled={busy}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendText(); } }} />
          <button className="primary" disabled={busy || !text.trim()} onClick={sendText}>Send</button>
        </div>
      )}
    </div>
  );
});

export default EncryptedChat;
