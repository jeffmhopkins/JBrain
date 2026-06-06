import { KeyboardEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { guidedStart, guidedSubmit, guidedTurn } from "../api";
import { linkifyAddresses, mapLinkRenderer } from "../util";
import { Icon } from "./Icon";

type Phase = "consent" | "asking" | "review" | "sent" | "closed" | "error";
interface Msg { role: "ai" | "me"; text: string; }

// The recipient's view of a guided AI intake link — styled to match the brain's
// own chat (full-height messages, rounded composer, replies that type out). The
// server returns the full sanitized reply (link-stripping needs the whole text),
// so the "streaming" feel is a client-side typewriter reveal.
export default function GuidedChat({ token, brainName, intro, consent, goal }: {
  token: string; brainName: string; intro?: string; consent?: string; goal?: string;
}) {
  const [phase, setPhase] = useState<Phase>("consent");
  const [name, setName] = useState(localStorage.getItem("jbrain_share_name") || "");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [doc, setDoc] = useState("");
  const [busy, setBusy] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [err, setErr] = useState("");
  // Typewriter reveal of the latest AI reply.
  const [typing, setTyping] = useState<string | null>(null);
  const [shown, setShown] = useState(0);

  const endRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, shown, thinking, phase]);

  // Grow the compose box as you type (mirrors the brain's composer).
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    const max = Math.round((window.visualViewport?.height ?? window.innerHeight) * 0.4);
    el.style.height = Math.min(el.scrollHeight, max) + "px";
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [input]);

  // Drive the typewriter: reveal a few chars per tick, then commit the message.
  useEffect(() => {
    if (typing == null) return;
    if (shown >= typing.length) {
      const full = typing;
      setMsgs((m) => [...m, { role: "ai", text: full }]);
      setTyping(null); setShown(0);
      return;
    }
    const step = Math.max(1, Math.round(typing.length / 110));
    const id = window.setTimeout(() => setShown((s) => Math.min(typing!.length, s + step)), 16);
    return () => clearTimeout(id);
  }, [typing, shown]);

  function reveal(text: string) { setTyping(text); setShown(0); }

  function applyTurn(r: any) {
    setThinking(false);
    if (r.phase === "error") { setErr(r.message || "Something went wrong."); return; }
    if (r.phase === "ended") { if (r.message) reveal(r.message); setPhase("closed"); return; }
    if (r.phase === "review") { setDoc(r.document || ""); setPhase("review"); }
    else setPhase("asking");
    if (r.message) reveal(r.message);
  }

  async function begin() {
    if (!name.trim()) { alert("Please enter your name."); return; }
    localStorage.setItem("jbrain_share_name", name.trim());
    setBusy(true); setErr(""); setThinking(true); setPhase("asking");
    try {
      const r = await guidedStart(token, name.trim());
      if (r.resumed) {
        setThinking(false);
        setMsgs((r.transcript || []).map((t: any) => ({ role: t.role === "assistant" ? "ai" : "me", text: t.content })));
        if (r.phase === "review") { setDoc(r.document || ""); setPhase("review"); } else setPhase("asking");
      } else applyTurn(r);
    } catch (e: any) { setThinking(false); setErr(e?.message || "Couldn't start."); setPhase("error"); }
    finally { setBusy(false); }
  }

  async function send() {
    const text = input.trim();
    if (!text || busy || typing) return;
    setInput(""); setMsgs((m) => [...m, { role: "me", text }]); setBusy(true); setThinking(true); setErr("");
    try { applyTurn(await guidedTurn(token, text)); }
    catch (e: any) { setThinking(false); setErr(e?.message || "Couldn't send — try again."); }
    finally { setBusy(false); }
  }

  async function finish() {
    setBusy(true); setErr("");
    try { await guidedSubmit(token); setPhase("sent"); }
    catch (e: any) { setErr(e?.message || "Couldn't submit."); }
    finally { setBusy(false); }
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  }

  // --- Consent landing (a centered card, like the bind landing) -----------
  if (phase === "consent") return (
    <div className="share-page"><div className="share-card">
      <div className="share-head">
        <span className="brand">{brainName}<span className="dot">.</span></span>
        <span className="badge">Shared · Guided intake</span>
      </div>
      <h2 style={{ marginTop: 12 }}>{goal ? goal[0].toUpperCase() + goal.slice(1) : "A few questions"}</h2>
      {intro && <p>{intro}</p>}
      {consent && <p className="muted" style={{ fontSize: 13 }}>{consent}</p>}
      <input placeholder="Your name *" value={name} onChange={(e) => setName(e.target.value)}
             onKeyDown={(e) => { if (e.key === "Enter") begin(); }} />
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={busy} onClick={begin}>{busy ? "…" : "Begin"}</button>
      </div>
    </div></div>
  );

  // --- The interview itself (matches the brain chat layout) ---------------
  const head = (
    <div className="guided-head">
      <span className="brand">{brainName}<span className="dot">.</span></span>
      <span className="badge">Shared · Guided intake</span>
    </div>
  );

  return (
    <div className="guided-wrap">
      {head}
      <div className="messages">
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role === "me" ? "user" : "assistant"}`}>
            {m.role === "ai"
              ? <div className="md msg-md"><ReactMarkdown components={{ a: mapLinkRenderer }}>{linkifyAddresses(m.text)}</ReactMarkdown></div>
              : m.text}
          </div>
        ))}
        {typing != null && (
          <div className="msg assistant"><div className="md msg-md"><ReactMarkdown>{typing.slice(0, shown)}</ReactMarkdown></div></div>
        )}
        {thinking && (
          <div className="chat-status" aria-live="polite">
            <span className="typing-dots"><span /><span /><span /></span>
            <span className="chat-status-text">Thinking…</span>
          </div>
        )}
        {phase === "review" && (
          <div className="msg assistant guided-doc-bubble"><div className="md msg-md"><ReactMarkdown components={{ a: mapLinkRenderer }}>{linkifyAddresses(doc)}</ReactMarkdown></div></div>
        )}
        {phase === "sent" && (
          <div className="msg assistant"><strong>Thank you, {name}!</strong><br />Your answers were sent to {brainName} for review.</div>
        )}
        {err && <div className="msg assistant share-error">{err}</div>}
        <div ref={endRef} />
      </div>

      {phase === "review" ? (
        <div className="composer-box">
          <div className="muted" style={{ fontSize: 13, marginBottom: 8 }}>Review the summary above, then send it to {brainName}.</div>
          <div className="row" style={{ gap: 8 }}>
            <button className="primary" disabled={busy} onClick={finish}>Looks good — send</button>
            <button className="ghost" disabled={busy} onClick={() => setPhase("asking")}>Add more</button>
          </div>
        </div>
      ) : phase === "sent" || phase === "closed" ? null : (
        <div className="composer-box">
          <textarea ref={taRef} rows={1} placeholder="Type your answer…" value={input}
                    onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} />
          <div className="composer-row">
            <span className="spacer" />
            <button className="icon-btn send" title="Send" onClick={send}
                    disabled={busy || !!typing || !input.trim()}><Icon name="send" /></button>
          </div>
        </div>
      )}
    </div>
  );
}
