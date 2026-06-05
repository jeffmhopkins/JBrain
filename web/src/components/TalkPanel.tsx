import { useEffect, useState } from "react";
import { get, post } from "../api";

interface Talk { id: number; kind: string; body: string; author: string; created_at: string; resolved_at: string | null; resolution?: string | null; }

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

  const open = items.filter((t) => !t.resolved_at);
  const done = items.filter((t) => t.resolved_at);
  // Keep the actionable items (owner directives, conflicts/questions/todos) up top; fold
  // the inert informational 'note'/'decision' logs behind a summary so they never crowd them.
  const PRIORITY = new Set(["directive", "conflict", "question", "todo"]);
  const primary = open.filter((t) => PRIORITY.has(t.kind) || t.author === "user");
  const minor = open.filter((t) => !PRIORITY.has(t.kind) && t.author !== "user");

  return (
    <div style={{ marginTop: 20 }}>
      <h3 style={{ marginBottom: 4 }}>AI talk</h3>
      <p className="muted" style={{ fontSize: 11, marginTop: 0 }}>
        The AI's reasoning for this article. Open items are worked through the Review inbox and
        the maintenance pass — they clear when the issue is actually handled, not by a click.
        Add a directive to steer the next pass.
      </p>

      {primary.map((t) => (
        <div key={t.id} className="talk-item">
          <span title={t.kind}>{KIND_ICON[t.kind] || "•"}</span>
          <span className="talk-text">{t.body}{t.author === "user" && <em className="muted"> — you</em>}</span>
        </div>
      ))}
      {open.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No open items.</p>}

      {minor.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>
            {minor.length} note{minor.length > 1 ? "s" : ""}
          </summary>
          {minor.map((t) => (
            <div key={t.id} className="talk-item muted">
              <span title={t.kind}>{KIND_ICON[t.kind] || "•"}</span>
              <span className="talk-text">{t.body}</span>
            </div>
          ))}
        </details>
      )}

      <div className="talk-add">
        <select className="modal-select" value={kind} onChange={(e) => setKind(e.target.value)}>
          {ADD_KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <input className="modal-input" placeholder="Add a note for the next pass…" value={body}
               onChange={(e) => setBody(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} />
        <button className="ghost" onClick={add}>Add</button>
      </div>

      {done.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>{done.length} resolved</summary>
          {done.map((t) => (
            <div key={t.id} className="talk-item muted">
              <span>{KIND_ICON[t.kind] || "•"}</span>
              <span className="talk-text">
                <span style={{ textDecoration: "line-through" }}>{t.body}</span>
                {t.resolution && <em style={{ display: "block", textDecoration: "none" }}>→ {t.resolution}</em>}
              </span>
            </div>
          ))}
        </details>
      )}
    </div>
  );
}
