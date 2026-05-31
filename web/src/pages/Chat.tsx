import { FormEvent, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { createEntry, post, streamChat } from "../api";
import { useGeo, useIsDesktop, useOnline } from "../hooks";
import StagingPanel from "../components/StagingPanel";
import { Icon } from "../components/Icon";

interface Msg { role: "user" | "assistant"; content: string; }
type Mode = "entry" | "assisted" | "research";

const MODES: { key: Mode; label: string; hint: string; icon: string }[] = [
  { key: "entry", label: "Entry", hint: "Store text directly as a note.", icon: "plus" },
  { key: "assisted", label: "Assisted", hint: "Talk it out; I propose the note.", icon: "robot" },
  { key: "research", label: "Research", hint: "Ask questions across your brain (read-only).", icon: "search" },
];

const PLACEHOLDER: Record<Mode, string> = {
  entry: "Write an entry — it's saved directly as a note.",
  assisted: "Talk to your brain…",
  research: "Ask your brain… (read-only)",
};

export default function Chat() {
  const isDesktop = useIsDesktop();
  const online = useOnline();
  const geo = useGeo();
  const [sp] = useSearchParams();
  // Desktop has its own mode selector; phone is driven by the shell's top buttons (?m=).
  const [deskMode, setDeskMode] = useState<Mode>(() => (localStorage.getItem("jbrain_mode") as Mode) || "entry");
  const mode: Mode = isDesktop ? deskMode : ((sp.get("m") as Mode) || "entry");

  const [convId, setConvId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [entries, setEntries] = useState<{ title: string; slug: string }[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stagingTick, setStagingTick] = useState(0);
  const [applied, setApplied] = useState<{ id: number; summary: string; undone?: boolean }[]>([]);
  const endRef = useRef<HTMLDivElement>(null);

  function pickDesktopMode(m: Mode) { setDeskMode(m); localStorage.setItem("jbrain_mode", m); }

  // A fresh conversation when entering / switching a chat mode (entry needs none).
  async function newConversation() {
    const { id } = await post("/api/chat/conversations");
    setConvId(id); setMessages([]); setApplied([]);
  }
  useEffect(() => {
    if (mode === "entry") return;
    newConversation();
  }, [mode]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, entries]);

  async function undo(id: number) {
    await post(`/api/staging/${id}/undo`);
    setApplied((a) => a.map((x) => (x.id === id ? { ...x, undone: true } : x)));
  }

  async function send(e: FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || streaming || busy || !online) return;
    setInput("");
    const coords = geo.enabled ? geo.coords : null;

    if (mode === "entry") {
      setBusy(true);
      try {
        const r = await createEntry(text, undefined, coords);
        setEntries((xs) => [{ title: r.title, slug: r.slug }, ...xs]);
      } finally { setBusy(false); }
      return;
    }

    if (!convId) return;
    setMessages((m) => [...m, { role: "user", content: text }, { role: "assistant", content: "" }]);
    setStreaming(true);
    try {
      await streamChat(convId, text, (ev) => {
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

  return (
    <div className="content chat-wrap">
      {isDesktop && (
        <div style={{ marginBottom: 4 }}>
          <div className="row" style={{ gap: 6 }}>
            {MODES.map((m) => (
              <button key={m.key} className={mode === m.key ? "primary" : "ghost"} onClick={() => pickDesktopMode(m.key)}
                      style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <Icon name={m.icon} size={16} /> {m.label}
              </button>
            ))}
          </div>
          <p className="muted" style={{ fontSize: 12, margin: "6px 2px 0" }}>{MODES.find((m) => m.key === mode)!.hint}</p>
        </div>
      )}

      {/* The area ABOVE the composer is the only thing that changes between modes. */}
      <div className="messages">
        {mode === "entry" ? (
          entries.length === 0
            ? <div className="msg assistant muted">Type below and press Send — it's saved straight to your wiki.</div>
            : entries.map((en, i) => (
                <Link key={i} to={`/note/${en.slug}`} className="msg user" style={{ textDecoration: "none" }}>
                  Saved: {en.title}
                </Link>
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

      {/* Shared composer — identical across Entry / Assisted / Research. */}
      <form className="composer" onSubmit={send}>
        <button type="button" className={geo.enabled ? "primary" : "ghost"} onClick={geo.toggle}
                title={geo.enabled ? "Location on" : "Tag entries with your location"} style={{ padding: "0 12px" }}>
          <Icon name="pin" />
        </button>
        <textarea
          rows={1}
          placeholder={online ? PLACEHOLDER[mode] : "Offline — reconnect to continue"}
          value={input} disabled={!online}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e); } }}
        />
        <button className="primary" type="submit" disabled={streaming || busy || !online}>Send</button>
      </form>
    </div>
  );
}
