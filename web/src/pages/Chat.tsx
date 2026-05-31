import { FormEvent, useEffect, useRef, useState } from "react";
import { get, post, streamChat } from "../api";
import { useIsDesktop, useOnline } from "../hooks";
import StagingPanel from "../components/StagingPanel";

interface Msg { role: "user" | "assistant"; content: string; }

export default function Chat() {
  const isDesktop = useIsDesktop();
  const online = useOnline();
  const [convId, setConvId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [stagingTick, setStagingTick] = useState(0);
  const endRef = useRef<HTMLDivElement>(null);

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
        } else if (ev.type === "error") {
          setMessages((m) => {
            const copy = [...m];
            copy[copy.length - 1] = { role: "assistant", content: `⚠️ ${ev.message}` };
            return copy;
          });
        }
      });
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
      {!isDesktop && <StagingPanel tick={stagingTick} onChange={() => setStagingTick((t) => t + 1)} />}
      <form className="composer" onSubmit={send}>
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
