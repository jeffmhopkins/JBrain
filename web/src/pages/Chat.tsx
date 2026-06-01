import { FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { createEntry, post, streamChat, uploadAttachment } from "../api";
import { useGeo, useOnline } from "../hooks";
import StagingPanel from "../components/StagingPanel";
import { Icon } from "../components/Icon";
import { makeLinkRenderer, renderWikiLinks } from "../util";

interface Msg { role: "user" | "assistant"; content: string; }
type Mode = "entry" | "assisted" | "research";

const MODES: { key: Mode; label: string; icon: string }[] = [
  { key: "entry", label: "Entry", icon: "plus" },
  { key: "assisted", label: "Assisted", icon: "robot" },
  { key: "research", label: "Research", icon: "search" },
];
const PLACEHOLDER: Record<Mode, string> = {
  entry: "Write an entry… (the first line becomes its title)",
  assisted: "Talk it out…",
  research: "Ask your brain… (read-only)",
};

export default function Chat() {
  const online = useOnline();
  const geo = useGeo();
  const navigate = useNavigate();
  const [mode, setMode] = useState<Mode>(() => (localStorage.getItem("jbrain_mode") as Mode) || "entry");
  const [menuOpen, setMenuOpen] = useState(false);

  const [convId, setConvId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [entries, setEntries] = useState<{ text: string; title: string; slug: string }[]>([]);
  const [input, setInput] = useState("");
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stagingTick, setStagingTick] = useState(0);
  const [applied, setApplied] = useState<{ id: number; summary: string; undone?: boolean }[]>([]);

  const endRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  // Only auto-follow new content when the user is already at the bottom; if they
  // scroll up to read, leave them there even as the reply streams in.
  const atBottomRef = useRef(true);
  function onMessagesScroll() {
    const el = scrollRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  // Grow the compose box upward as you type, up to ~half the visible height,
  // then scroll inside it.
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    const max = Math.round((window.visualViewport?.height ?? window.innerHeight) * 0.5);
    el.style.height = Math.min(el.scrollHeight, max) + "px";
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [input]);

  function pick(m: Mode) { setMode(m); localStorage.setItem("jbrain_mode", m); setMenuOpen(false); }

  async function newConversation() {
    const { id } = await post("/api/chat/conversations");
    setConvId(id); setMessages([]); setApplied([]);
  }
  useEffect(() => { if (mode !== "entry") newConversation(); }, [mode]);
  useEffect(() => {
    if (atBottomRef.current) endRef.current?.scrollIntoView({ behavior: "auto" });
  }, [messages, entries]);

  async function undo(id: number) {
    await post(`/api/staging/${id}/undo`);
    setApplied((a) => a.map((x) => (x.id === id ? { ...x, undone: true } : x)));
  }

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if ((!text && !pendingFile) || streaming || busy || !online) return;
    const coords = geo.enabled ? geo.coords : null;
    const file = pendingFile;
    setInput(""); setPendingFile(null);

    if (mode === "entry") {
      setBusy(true);
      try {
        const r = await createEntry(text || (file ? file.name : "Untitled"), undefined, coords);
        if (file) await uploadAttachment(r.slug, file);
        setEntries((xs) => [...xs, { text: text || (file ? `📎 ${file.name}` : ""), title: r.title, slug: r.slug }]);
      } catch (err) {
        // Don't silently lose the entry: put the text back and tell the user.
        setInput(text);
        if (file) setPendingFile(file);
        alert("Couldn't save entry: " + (err instanceof Error ? err.message : "please try again."));
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
    atBottomRef.current = true;   // sending re-engages follow, so you see your message + reply
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
      <div className="messages" ref={scrollRef} onScroll={onMessagesScroll}>
        {mode === "entry" ? (
          entries.length === 0
            ? <div className="msg assistant muted">Type below and Send — it's saved straight to your wiki.</div>
            : entries.map((en, i) => (
                <div key={i} style={{ display: "contents" }}>
                  {en.text && <div className="msg user">{en.text}</div>}
                  <Link to={`/note/${en.slug}`} className="saved-chip"><Icon name="check" size={14} /> Saved: {en.title}</Link>
                </div>
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
              <div key={i} className={`msg ${m.role}`}>
                {m.role === "assistant" && m.content ? (
                  <div className="md msg-md">
                    <ReactMarkdown components={{ a: makeLinkRenderer(navigate) }}>{renderWikiLinks(m.content)}</ReactMarkdown>
                  </div>
                ) : (
                  m.content || (streaming && i === messages.length - 1 ? "…" : "")
                )}
              </div>
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
          ref={taRef}
          rows={1}
          placeholder={online ? PLACEHOLDER[mode] : "Offline — reconnect to continue"}
          value={input} disabled={!online}
          onChange={(e) => setInput(e.target.value)}
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
                     onChange={(e) => {
                       const f = e.target.files?.[0];
                       if (f) { if (f.size > 10 * 1024 * 1024) alert("File too large (10 MB max)."); else setPendingFile(f); }
                       e.currentTarget.value = "";
                     }} />
              <button className="icon-btn" title="Attach file" onClick={() => fileRef.current?.click()}><Icon name="clip" /></button>
            </>
          )}
          <button className="icon-btn send" title="Send" onClick={() => send()}
                  disabled={streaming || busy || !online || (!input.trim() && !pendingFile)}><Icon name="send" /></button>
        </div>
      </div>
    </div>
  );
}
