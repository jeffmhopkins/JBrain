import { KeyboardEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { researchStart, researchTurn } from "../api";
import { Icon } from "./Icon";

type Phase = "consent" | "chatting" | "ended";
interface Msg { role: "ai" | "me"; text: string; }

// Recipient view of a research Q&A link: ask questions answered by a scope-bounded
// AI. Mirrors the brain's chat layout (and GuidedChat), but the recipient asks and
// the AI answers (the inverse of guided intake).
export default function ResearchChat({ token, brainName, intro }: {
  token: string; brainName: string; intro?: string;
}) {
  const [phase, setPhase] = useState<Phase>("consent");
  const [name, setName] = useState(localStorage.getItem("jbrain_share_name") || "");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [err, setErr] = useState("");

  const endRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, thinking, phase]);
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    const max = Math.round((window.visualViewport?.height ?? window.innerHeight) * 0.4);
    el.style.height = Math.min(el.scrollHeight, max) + "px";
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [input]);

  async function begin() {
    if (!name.trim()) { alert("Please enter your name."); return; }
    localStorage.setItem("jbrain_share_name", name.trim());
    setBusy(true); setErr("");
    try {
      const r = await researchStart(token, name.trim());
      setMsgs((r.transcript || []).map((t: any) => ({ role: t.role === "assistant" ? "ai" : "me", text: t.content })));
      setPhase("chatting");
    } catch (e: any) { setErr(e?.message || "Couldn't start."); }
    finally { setBusy(false); }
  }

  async function send() {
    const text = input.trim();
    if (!text || busy || thinking) return;
    setInput(""); setMsgs((m) => [...m, { role: "me", text }]); setThinking(true); setErr("");
    try {
      const r = await researchTurn(token, text);
      setMsgs((m) => [...m, { role: "ai", text: r.message }]);
      if (r.phase === "ended") setPhase("ended");
    } catch (e: any) { setErr(e?.message || "Couldn't send — please try again."); }
    finally { setThinking(false); }
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  }

  if (phase === "consent") return (
    <div className="share-page"><div className="share-card">
      <div className="share-head">
        <span className="brand">{brainName}<span className="dot">.</span></span>
        <span className="badge">Guided · Research</span>
      </div>
      <h2 style={{ marginTop: 12 }}>Ask {brainName}’s records</h2>
      {intro && <p>{intro}</p>}
      <p className="muted" style={{ fontSize: 13 }}>
        You’re chatting with an AI that can answer questions from a specific set of records {brainName}
        {" "}shared. It only reads what they approved, and your conversation is logged for them. Not professional advice.
      </p>
      <input placeholder="Your name *" value={name} onChange={(e) => setName(e.target.value)}
             onKeyDown={(e) => { if (e.key === "Enter") begin(); }} />
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={busy} onClick={begin}>{busy ? "…" : "Start"}</button>
      </div>
    </div></div>
  );

  return (
    <div className="guided-wrap">
      <div className="guided-head">
        <span className="brand">{brainName}<span className="dot">.</span></span>
        <span className="badge">Guided · Research</span>
      </div>
      <div className="messages">
        {msgs.length === 0 && (
          <div className="msg assistant muted">Ask a question about the records {brainName} shared with you.</div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role === "me" ? "user" : "assistant"}`}>
            {m.role === "ai" ? <div className="md msg-md"><ReactMarkdown>{m.text}</ReactMarkdown></div> : m.text}
          </div>
        ))}
        {thinking && (
          <div className="chat-status" aria-live="polite">
            <span className="typing-dots"><span /><span /><span /></span>
            <span className="chat-status-text">Thinking…</span>
          </div>
        )}
        {phase === "ended" && <div className="msg assistant muted">This conversation has ended. Thanks!</div>}
        {err && <div className="msg assistant" style={{ color: "var(--danger)" }}>{err}</div>}
        <div ref={endRef} />
      </div>
      {phase !== "ended" && (
        <div className="composer-box">
          <textarea ref={taRef} rows={1} placeholder="Ask a question…" value={input}
                    onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} />
          <div className="composer-row">
            <span className="spacer" />
            <button className="icon-btn send" title="Send" onClick={send}
                    disabled={thinking || !input.trim()}><Icon name="send" /></button>
          </div>
        </div>
      )}
    </div>
  );
}
