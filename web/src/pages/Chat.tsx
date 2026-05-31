import { FormEvent, useEffect, useRef, useState } from "react";
import { get, post, streamChat } from "../api";
import { useGeo, useIsDesktop, useOnline } from "../hooks";
import StagingPanel from "../components/StagingPanel";

interface Msg { role: "user" | "assistant"; content: string; }

export default function Chat() {
  const isDesktop = useIsDesktop();
  const online = useOnline();
  const geo = useGeo();
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
    setConvId(id);
    setMessages([]);
  }

  useEffect(() => { newConversation(); }, []);
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
      }, geo.enabled ? geo.coords : null);
    } finally {
      setStreaming(false);
      setStagingTick((t) => t + 1);
    }
  }

  const conversation = (
    <div className="content chat-wrap">
      <div className="row" style={{ marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Think out loud</h2>
        <div className="spacer" />
        <button className="ghost" onClick={newConversation}>+ New chat</button>
      </div>
      <div className="messages">
        {messages.length === 0 && (
          <div className="msg assistant muted">
            Start talking — vent an idea, a problem, a plan. I’ll ask questions and,
            when a thought is ready, propose notes for you to confirm.
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>{m.content || (streaming && i === messages.length - 1 ? "…" : "")}</div>
        ))}
        <div ref={endRef} />
      </div>
      {applied.length > 0 && (
        <div style={{ margin: "8px 0" }}>
          {applied.map((a) => (
            <div key={a.id} className="applied-chip">
              <span>✓ {a.summary}</span>
              {a.undone ? (
                <span className="muted" style={{ fontSize: 12 }}>undone</span>
              ) : (
                <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => undo(a.id)}>Undo</button>
              )}
            </div>
          ))}
        </div>
      )}
      {!isDesktop && <StagingPanel tick={stagingTick} onChange={() => setStagingTick((t) => t + 1)} />}
      <form className="composer" onSubmit={send}>
        <button
          type="button"
          className={geo.enabled ? "primary" : "ghost"}
          title={geo.enabled
            ? (geo.coords ? `Location on (${geo.coords.lat}, ${geo.coords.lon})` : "Location on (acquiring…)")
            : "Tag entries with your location"}
          onClick={geo.toggle}
          style={{ padding: "0 12px" }}
        >📍</button>
        <textarea
          rows={1}
          placeholder={online ? "Talk to your brain…" : "Offline — reconnect to chat"}
          value={input}
          disabled={!online}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e); } }}
        />
        <button className="primary" type="submit" disabled={streaming || !online}>Send</button>
      </form>
    </div>
  );

  if (!isDesktop) return conversation;
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
