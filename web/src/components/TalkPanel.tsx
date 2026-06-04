import { useEffect, useState } from "react";
import { get, post } from "../api";

interface Talk { id: number; kind: string; body: string; author: string; created_at: string; resolved_at: string | null; }

const KIND_ICON: Record<string, string> = {
  decision: "🧠", conflict: "⚠️", question: "❓", todo: "☑️", directive: "📌", note: "📝",
};
const ADD_KINDS = [["note", "Note"], ["directive", "Directive"], ["question", "Question"], ["todo", "TODO"]];

// The article "talk" panel — Wikipedia-Talk-style memory the KB maintenance loop reads
// and writes. Shows the AI's decisions/conflicts/questions and lets you add a directive
// or note (and resolve items). Only meaningful on kb/ articles.
export default function TalkPanel({ slug }: { slug: string }) {
  const [items, setItems] = useState<Talk[]>([]);
  const [kind, setKind] = useState("directive");
  const [body, setBody] = useState("");

  function load() { get<Talk[]>(`/api/notes/${slug}/talk`).then(setItems).catch(() => setItems([])); }
  useEffect(load, [slug]);

  async function add() {
    if (!body.trim()) return;
    await post(`/api/notes/${slug}/talk`, { kind, body });
    setBody(""); load();
  }
  async function resolve(id: number) { await post(`/api/notes/${slug}/talk/${id}/resolve`, {}); load(); }

  const open = items.filter((t) => !t.resolved_at);
  const done = items.filter((t) => t.resolved_at);

  return (
    <div style={{ marginTop: 20 }}>
      <h3 style={{ marginBottom: 4 }}>AI talk</h3>
      <p className="muted" style={{ fontSize: 11, marginTop: 0 }}>
        Decisions, conflicts &amp; questions the maintenance pass reads. Add a directive to steer it.
      </p>

      {open.map((t) => (
        <div key={t.id} className="talk-item">
          <span title={t.kind}>{KIND_ICON[t.kind] || "•"}</span>
          <span style={{ flex: 1 }}>{t.body}{t.author === "user" && <em className="muted"> — you</em>}</span>
          <button className="ghost talk-resolve" title="Resolve" onClick={() => resolve(t.id)}>✓</button>
        </div>
      ))}
      {open.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No open items.</p>}

      <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
        <select className="modal-select" value={kind} onChange={(e) => setKind(e.target.value)} style={{ flex: "0 0 auto" }}>
          {ADD_KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <input className="modal-input" placeholder="Add a note for the next pass…" value={body}
               onChange={(e) => setBody(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} style={{ flex: 1 }} />
        <button className="ghost" onClick={add}>Add</button>
      </div>

      {done.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>{done.length} resolved</summary>
          {done.map((t) => (
            <div key={t.id} className="talk-item muted" style={{ textDecoration: "line-through" }}>
              <span>{KIND_ICON[t.kind] || "•"}</span><span style={{ flex: 1 }}>{t.body}</span>
            </div>
          ))}
        </details>
      )}
    </div>
  );
}
