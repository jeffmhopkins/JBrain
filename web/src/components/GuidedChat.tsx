import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { guidedStart, guidedSubmit, guidedTurn } from "../api";

type Phase = "consent" | "asking" | "review" | "sent" | "error";
interface Msg { role: "ai" | "me"; text: string; }

// The recipient's view of a guided AI intake link. Talks to the isolated
// interview AI; on completion the AI drafts a document the recipient confirms
// before it goes to the owner for final approval.
export default function GuidedChat({ token, brainName, intro, consent, goal }: {
  token: string; brainName: string; intro?: string; consent?: string; goal?: string;
}) {
  const [phase, setPhase] = useState<Phase>("consent");
  const [name, setName] = useState(localStorage.getItem("jbrain_share_name") || "");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [doc, setDoc] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, phase]);

  function applyTurn(r: any) {
    if (r.message) setMsgs((m) => [...m, { role: "ai", text: r.message }]);
    if (r.phase === "review") { setDoc(r.document || ""); setPhase("review"); }
    else if (r.phase === "error") setErr(r.message || "Something went wrong.");
    else setPhase("asking");
  }

  async function begin() {
    if (!name.trim()) { alert("Please enter your name."); return; }
    localStorage.setItem("jbrain_share_name", name.trim());
    setBusy(true); setErr("");
    try {
      const r = await guidedStart(token, name.trim());
      if (r.resumed) {
        setMsgs((r.transcript || []).map((t: any) => ({ role: t.role === "assistant" ? "ai" : "me", text: t.content })));
        if (r.phase === "review") { setDoc(r.document || ""); setPhase("review"); } else setPhase("asking");
      } else applyTurn(r);
    } catch (e: any) { setErr(e?.message || "Couldn't start."); setPhase("error"); }
    finally { setBusy(false); }
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput(""); setMsgs((m) => [...m, { role: "me", text }]); setBusy(true); setErr("");
    try { applyTurn(await guidedTurn(token, text)); }
    catch (e: any) { setErr(e?.message || "Couldn't send — try again."); }
    finally { setBusy(false); }
  }

  async function finish() {
    setBusy(true); setErr("");
    try { await guidedSubmit(token); setPhase("sent"); }
    catch (e: any) { setErr(e?.message || "Couldn't submit."); }
    finally { setBusy(false); }
  }

  if (phase === "consent") return (
    <div className="share-page"><div className="share-card">
      <div className="share-head">
        <span className="brand">{brainName}<span className="dot">.</span></span>
        <span className="badge">Guided · AI</span>
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

  return (
    <div className="share-page"><div className="share-card">
      <div className="share-head">
        <span className="brand">{brainName}<span className="dot">.</span></span>
        <span className="badge">Guided · AI</span>
      </div>
      {err && <p style={{ color: "var(--danger)", fontSize: 13 }}>{err}</p>}
      <div className="guided-chat">
        {msgs.map((m, i) => <div key={i} className={"msg " + (m.role === "me" ? "user" : "assistant")}>{m.text}</div>)}
        <div ref={endRef} />
      </div>
      {phase === "review" ? (
        <div>
          <div className="share-banner">Here’s what I put together — review it, then send it to {brainName}.</div>
          <div className="md guided-doc"><ReactMarkdown>{doc}</ReactMarkdown></div>
          <div className="row" style={{ marginTop: 10, gap: 8 }}>
            <button className="primary" disabled={busy} onClick={finish}>Looks good — send</button>
            <button className="ghost" disabled={busy} onClick={() => setPhase("asking")}>Add more</button>
          </div>
        </div>
      ) : phase === "sent" ? (
        <div className="share-sent">
          <h3>Thank you, {name}!</h3>
          <p className="muted">Your answers were sent to {brainName} for review.</p>
        </div>
      ) : (
        <div className="row" style={{ marginTop: 10, gap: 8 }}>
          <input placeholder="Type your answer…" value={input} onChange={(e) => setInput(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") send(); }} disabled={busy} />
          <button className="primary" disabled={busy} onClick={send}>Send</button>
        </div>
      )}
    </div></div>
  );
}
