import { FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { createEntry, post, streamChat, uploadAttachment } from "../api";
import { useGeo, useOnline } from "../hooks";
import StagingPanel from "../components/StagingPanel";
import { Icon } from "../components/Icon";

interface Msg { role: "user" | "assistant"; content: string; }
type Mode = "entry" | "assisted" | "research";

const MODES: { key: Mode; label: string; icon: string }[] = [
  { key: "entry", label: "Entry", icon: "plus" },
  { key: "assisted", label: "Assisted", icon: "robot" },
  { key: "research", label: "Research", icon: "search" },
];
const PLACEHOLDER: Record<Mode, string> = {
  entry: "Write an entry…",
  assisted: "Talk it out…",
  research: "Ask your brain… (read-only)",
};

export default function Chat() {
  const online = useOnline();
  const geo = useGeo();
  const [mode, setMode] = useState<Mode>(() => (localStorage.getItem("jbrain_mode") as Mode) || "entry");
  const [menuOpen, setMenuOpen] = useState(false);

  const [convId, setConvId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [entries, setEntries] = useState<{ title: string; slug: string }[]>([]);
  const [input, setInput] = useState("");
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stagingTick, setStagingTick] = useState(0);
  const [applied, setApplied] = useState<{ id: number; summary: string; undone?: boolean }[]>([]);
  const [listening, setListening] = useState(false);

  const endRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recRef = useRef<any>(null);

  function pick(m: Mode) { setMode(m); localStorage.setItem("jbrain_mode", m); setMenuOpen(false); }

  async function newConversation() {
    const { id } = await post("/api/chat/conversations");
    setConvId(id); setMessages([]); setApplied([]);
  }
  useEffect(() => { if (mode !== "entry") newConversation(); }, [mode]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, entries]);

  async function undo(id: number) {
    await post(`/api/staging/${id}/undo`);
    setApplied((a) => a.map((x) => (x.id === id ? { ...x, undone: true } : x)));
  }

  function toggleMic() {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) { alert("Voice input isn't supported in this browser."); return; }
    if (listening) { recRef.current?.stop(); return; }
    const r = new SR();
    recRef.current = r;
    r.lang = "en-US"; r.interimResults = true; r.continuous = true;
    const base = input ? input + " " : "";
    r.onresult = (e: any) => {
      let t = "";
      for (let i = 0; i < e.results.length; i++) t += e.results[i][0].transcript;
      setInput(base + t);
    };
    r.onend = () => setListening(false);
    r.onerror = () => setListening(false);
    r.start();
    setListening(true);
  }

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if ((!text && !pendingFile) || streaming || busy || !online) return;
    const coords = geo.enabled ? geo.coords : null;
    const file = pendingFile;
    setInput(""); setPendingFile(null);
    if (listening) recRef.current?.stop();

    if (mode === "entry") {
      setBusy(true);
      try {
        const r = await createEntry(text || (file ? file.name : "Untitled"), undefined, coords);
        if (file) await uploadAttachment(r.slug, file);
        setEntries((xs) => [{ title: r.title, slug: r.slug }, ...xs]);
      } finally { setBusy(false); }
      return;
    }

    if (!convId) return;
    let extra = "";
    if (mode === "assisted" && file) {
      // Save the file to a note so there's something to attach it to.
      const r = await createEntry(`Attached file: ${file.name}`, file.name.replace(/\.[^.]+$/, ""), coords);
      await uploadAttachment(r.slug, file);
      extra = `\n\n(I attached a file, saved as [[${r.title}]].)`;
    }
    const msg = (text + extra).trim();
    setMessages((m) => [...m, { role: "user", content: msg }, { role: "assistant", content: "" }]);
    setStreaming(true);
    try {
      await streamChat(convId, msg, (ev) => {
        if (ev.type === "token") {
          setMessages((m) => {
            const c = [...m];
            c[c.length - 1] = { role: "assistant", content: c[c.length - 1].content + (ev.text || "") };
            return c;
          });
        } else if (ev.type === "staging") {
          setStagingTick((t) => t + 1);
        } else if (ev.type === "applied" && ev.action) {
          setApplied((a) => [...a, ev.action!]);
        } else if (ev.type === "error") {
          setMessages((m) => {
            const c = [...m];
            c[c.length - 1] = { role: "assistant", content: `⚠️ ${ev.message}` };
            return c;
          });
        }
      }, coords, mode === "research" ? "research" : "assisted");
    } finally {
      setStreaming(false);
      if (mode === "assisted") setStagingTick((t) => t + 1);
    }
  }

  const cur = MODES.find((m) => m.key === mode)!;

  return (
    <div className="chat-wrap">
      <div className="messages">
        {mode === "entry" ? (
          entries.length === 0
            ? <div className="msg assistant muted">Type below and Send — it's saved straight to your wiki.</div>
            : entries.map((en, i) => (
                <Link key={i} to={`/note/${en.slug}`} className="msg user" style={{ textDecoration: "none" }}>Saved: {en.title}</Link>
              ))
        ) : (
          <>
            {messages.length === 0 && (
              <div className="msg assistant muted">
                {mode === "research"
                  ? "Ask anything about your notes — I only read; I won’t change anything."
                  : "Tell me what you want to capture. I’ll ask questions, then propose a note to confirm."}
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}>{m.content || (streaming && i === messages.length - 1 ? "…" : "")}</div>
            ))}
            {mode === "assisted" && applied.map((a) => (
              <div key={`a${a.id}`} className="applied-chip">
                <span>✓ {a.summary}</span>
                {a.undone
                  ? <span className="muted" style={{ fontSize: 12 }}>undone</span>
                  : <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => undo(a.id)}>Undo</button>}
              </div>
            ))}
            {mode === "assisted" && <StagingPanel tick={stagingTick} onChange={() => setStagingTick((t) => t + 1)} />}
          </>
        )}
        <div ref={endRef} />
      </div>

      {/* Compose box (rounded), shared across modes. */}
      <div className="composer-box">
        {pendingFile && (
          <div className="attach-chip">
            <Icon name="clip" size={14} /> {pendingFile.name}
            <button className="icon-btn" style={{ padding: 2 }} onClick={() => setPendingFile(null)}>✕</button>
          </div>
        )}
        <textarea
          rows={2}
          placeholder={online ? PLACEHOLDER[mode] : "Offline — reconnect to continue"}
          value={input} disabled={!online}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
        />
        <div className="composer-row">
          <span className="mode-wrap">
            <button className="mode-chip" onClick={() => setMenuOpen((o) => !o)}>
              <Icon name={cur.icon} size={18} /> {cur.label}
            </button>
            {menuOpen && (
              <div className="mode-menu">
                {MODES.map((m) => (
                  <button key={m.key} onClick={() => pick(m.key)}>
                    <Icon name={m.icon} size={16} /> {m.label}
                  </button>
                ))}
              </div>
            )}
          </span>
          <span className="spacer" />
          {mode !== "research" && (
            <>
              <input ref={fileRef} type="file" style={{ display: "none" }}
                     onChange={(e) => { const f = e.target.files?.[0]; if (f) setPendingFile(f); e.currentTarget.value = ""; }} />
              <button className="icon-btn" title="Attach file" onClick={() => fileRef.current?.click()}><Icon name="clip" /></button>
            </>
          )}
          <button className={"icon-btn" + (listening ? " active" : "")} title="Voice input" onClick={toggleMic}><Icon name="mic" /></button>
          <button className="icon-btn send" title="Send" onClick={() => send()}
                  disabled={streaming || busy || !online || (!input.trim() && !pendingFile)}><Icon name="send" /></button>
        </div>
      </div>
    </div>
  );
}
