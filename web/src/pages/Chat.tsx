import { FormEvent, useEffect, useRef, useState } from "react";
import { createEntry, post, streamChat } from "../api";
import { useGeo, useIsDesktop, useOnline } from "../hooks";
import StagingPanel from "../components/StagingPanel";

interface Msg { role: "user" | "assistant"; content: string; }
type Mode = "entry" | "assisted" | "research";

const MODES: { key: Mode; label: string; hint: string }[] = [
  { key: "entry", label: "Entry", hint: "Store text directly as a note." },
  { key: "assisted", label: "Assisted", hint: "Talk it out; I propose the note." },
  { key: "research", label: "Research", hint: "Ask questions across your brain (read-only)." },
];

export default function Chat() {
  const isDesktop = useIsDesktop();
  const geo = useGeo();
  const [mode, setMode] = useState<Mode>(() => (localStorage.getItem("jbrain_mode") as Mode) || "assisted");

  function pick(m: Mode) {
    setMode(m);
    localStorage.setItem("jbrain_mode", m);
  }

  return (
    <div className={isDesktop && mode === "assisted" ? "" : ""}>
      <div className="content" style={{ paddingBottom: 0 }}>
        <div className="row" style={{ gap: 6 }}>
          {MODES.map((m) => (
            <button key={m.key} className={mode === m.key ? "primary" : "ghost"} onClick={() => pick(m.key)}>
              {m.label}
            </button>
          ))}
        </div>
        <p className="muted" style={{ fontSize: 12, margin: "6px 2px 0" }}>
          {MODES.find((m) => m.key === mode)!.hint}
        </p>
      </div>
      {mode === "entry" ? <EntryForm geo={geo} /> : <ChatPane mode={mode} geo={geo} isDesktop={isDesktop} />}
    </div>
  );
}

function EntryForm({ geo }: { geo: ReturnType<typeof useGeo> }) {
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true); setMsg("");
    try {
      const r = await createEntry(text.trim(), title.trim(), geo.enabled ? geo.coords : null);
      setMsg(`Saved “${r.title}”.`);
      setTitle(""); setText("");
    } catch {
      setMsg("Save failed.");
    } finally { setBusy(false); }
  }

  return (
    <div className="content">
      <form onSubmit={save}>
        <input placeholder="Title (optional)" value={title} onChange={(e) => setTitle(e.target.value)} />
        <textarea
          rows={8} style={{ marginTop: 8 }}
          placeholder="Write your entry — it's stored directly as a note."
          value={text} onChange={(e) => setText(e.target.value)}
        />
        <div className="row" style={{ marginTop: 10, gap: 8 }}>
          <button
            type="button" className={geo.enabled ? "primary" : "ghost"} onClick={geo.toggle}
            title={geo.enabled ? "Location on" : "Tag this entry with your location"} style={{ padding: "0 12px" }}
          >📍</button>
          <button className="primary" type="submit" disabled={busy || !text.trim()}>Save entry</button>
          {msg && <span className="muted" style={{ fontSize: 13 }}>{msg}</span>}
        </div>
      </form>
    </div>
  );
}

function ChatPane({ mode, geo, isDesktop }: { mode: Mode; geo: ReturnType<typeof useGeo>; isDesktop: boolean }) {
  const online = useOnline();
  const research = mode === "research";
  const [convId, setConvId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [stagingTick, setStagingTick] = useState(0);
  const [applied, setApplied] = useState<{ id: number; summary: string; undone?: boolean }[]>([]);
  const endRef = useRef<HTMLDivElement>(null);

  async function undo(id: number) {
    await post(`/api/staging/${id}/undo`);
    setApplied((a) => a.map((x) => (x.id === id ? { ...x, undone: true } : x)));
  }
  async function newConversation() {
    const { id } = await post("/api/chat/conversations");
    setConvId(id); setMessages([]); setApplied([]);
  }
  // Fresh conversation per mode so assisted/research threads don't intermix.
  useEffect(() => { newConversation(); }, [mode]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  async function send(e: FormEvent) {
    e.preventDefault();
    if (!input.trim() || !convId || streaming) return;
    const text = input.trim();
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text }, { role: "assistant", content: "" }]);
    setStreaming(true);
    try {
      await streamChat(convId, text, (ev) => {
        if (ev.type === "token") {
          setMessages((m) => {
            const copy = [...m];
            copy[copy.length - 1] = { role: "assistant", content: copy[copy.length - 1].content + (ev.text || "") };
            return copy;
          });
        } else if (ev.type === "staging") {
          setStagingTick((t) => t + 1);
        } else if (ev.type === "applied" && ev.action) {
          setApplied((a) => [...a, ev.action!]);
        } else if (ev.type === "error") {
          setMessages((m) => {
            const copy = [...m];
            copy[copy.length - 1] = { role: "assistant", content: `⚠️ ${ev.message}` };
            return copy;
          });
        }
      }, geo.enabled ? geo.coords : null, research ? "research" : "assisted");
    } finally {
      setStreaming(false);
      if (!research) setStagingTick((t) => t + 1);
    }
  }

  const conversation = (
    <div className="content chat-wrap">
      <div className="row" style={{ margin: "8px 0" }}>
        <strong>{research ? "Research your brain" : "Think out loud"}</strong>
        <div className="spacer" />
        <button className="ghost" onClick={newConversation}>+ New</button>
      </div>
      <div className="messages">
        {messages.length === 0 && (
          <div className="msg assistant muted">
            {research
              ? "Ask anything about your notes — “what have I logged about sleep?”, “summarise my project decisions”. I only read; I won’t change anything."
              : "Tell me what you want to capture. I’ll ask questions, then propose a note for you to confirm."}
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>{m.content || (streaming && i === messages.length - 1 ? "…" : "")}</div>
        ))}
        <div ref={endRef} />
      </div>
      {!research && applied.length > 0 && (
        <div style={{ margin: "8px 0" }}>
          {applied.map((a) => (
            <div key={a.id} className="applied-chip">
              <span>✓ {a.summary}</span>
              {a.undone
                ? <span className="muted" style={{ fontSize: 12 }}>undone</span>
                : <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => undo(a.id)}>Undo</button>}
            </div>
          ))}
        </div>
      )}
      {!research && !isDesktop && <StagingPanel tick={stagingTick} onChange={() => setStagingTick((t) => t + 1)} />}
      <form className="composer" onSubmit={send}>
        <button type="button" className={geo.enabled ? "primary" : "ghost"} onClick={geo.toggle}
          title={geo.enabled ? "Location on" : "Tag entries with your location"} style={{ padding: "0 12px" }}>📍</button>
        <textarea
          rows={1}
          placeholder={online ? (research ? "Ask your brain…" : "Talk to your brain…") : "Offline — reconnect to chat"}
          value={input} disabled={!online}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e); } }}
        />
        <button className="primary" type="submit" disabled={streaming || !online}>Send</button>
      </form>
    </div>
  );

  if (research || !isDesktop) return conversation;
  return (
    <div className="with-rail">
      {conversation}
      <aside className="rail">
        <h3 style={{ marginTop: 0 }}>Staging area</h3>
        <p className="muted" style={{ fontSize: 13 }}>Proposed changes appear here. Nothing is saved until you apply it.</p>
        <StagingPanel tick={stagingTick} onChange={() => setStagingTick((t) => t + 1)} />
      </aside>
    </div>
  );
}
