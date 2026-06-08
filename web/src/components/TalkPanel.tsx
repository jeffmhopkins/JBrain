import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get, post, talkReply, talkDismiss, talkMaintainNow } from "../api";
import { useCapability } from "../capabilities";

interface Reply { id: number; author: string; body: string; created_at: string; }
interface Talk {
  id: number; kind: string; body: string; author: string; created_at: string;
  resolved_at: string | null; resolution?: string | null;
  is_correction?: number; source_note_slug?: string | null;
  replies?: Reply[];
}

const KIND_ICON: Record<string, string> = {
  decision: "🧠", conflict: "⚠️", question: "❓", todo: "☑️", directive: "📌", note: "📝",
  correction: "✅",
};
const ADD_KINDS = [
  ["note", "Note"], ["directive", "Directive"], ["correction", "Correction"],
  ["question", "Question"], ["todo", "TODO"],
];

// A promoted correction's badge: links to the dated truth note it spawned, or shows the
// note was deleted (FK set null) — either way the talk row stays as a record.
function CorrectionBadge({ t }: { t: Talk }) {
  if (!t.is_correction) return null;
  const style = { marginLeft: 6, fontSize: 10, background: "#d4edda", color: "#155724",
                  borderRadius: 3, padding: "1px 5px", whiteSpace: "nowrap" as const };
  return t.source_note_slug
    ? <Link to={`/note/${t.source_note_slug}`} style={style} title="Promoted to a truth-layer note">✓ truth note →</Link>
    : <span style={{ ...style, background: "#eee", color: "#777" }} title="The promoted note was deleted">✓ truth (note deleted)</span>;
}

// The article "talk" panel — Wikipedia-Talk-style memory the KB maintenance loop reads
// and writes. Shows the AI's decisions/conflicts/questions and lets you add a directive,
// REPLY to an item (steering the next pass), DISMISS an item you've handled, or run a
// maintenance pass on demand. Only meaningful on kb/ articles.
export default function TalkPanel({ slug }: { slug: string }) {
  const [items, setItems] = useState<Talk[]>([]);
  const [kind, setKind] = useState("directive");
  const [body, setBody] = useState("");
  const [flash, setFlash] = useState("");
  // Per-item reply composer state, the in-flight guard, and the on-demand-pass status.
  const [replyFor, setReplyFor] = useState<number | null>(null);
  const [replyText, setReplyText] = useState("");
  const [busy, setBusy] = useState(false);
  const [maintaining, setMaintaining] = useState(false);
  const llm = useCapability("llm");   // "Resolve with AI now" runs the LLM maintenance pass
  const [maintMsg, setMaintMsg] = useState("");

  function load() { get<Talk[]>(`/api/notes/${slug}/talk`).then(setItems).catch(() => setItems([])); }
  useEffect(load, [slug]);

  async function add() {
    if (!body.trim()) return;
    const r = await post<{ id: number | null; promoted?: { slug: string; title: string } | null }>(
      `/api/notes/${slug}/talk`, { kind, body });
    // Instant confirmation that a correction entered the truth layer as a real note.
    setFlash(r?.promoted ? `✓ Saved as a source-of-truth note (${r.promoted.title}).` : "");
    setBody(""); load();
  }

  async function sendReply(id: number) {
    const text = replyText.trim();
    if (!text || busy) return;
    setBusy(true);
    try { await talkReply(slug, id, text); setReplyText(""); setReplyFor(null); load(); }
    finally { setBusy(false); }
  }

  async function dismiss(t: Talk) {
    if (busy) return;
    if (!window.confirm("Dismiss this item? It will stop being worked by the maintenance pass.")) return;
    setBusy(true);
    try { await talkDismiss(slug, t.id); load(); }
    catch (e: any) { alert(e?.message || "Couldn't dismiss."); }
    finally { setBusy(false); }
  }

  async function maintainNow() {
    if (maintaining) return;
    setMaintaining(true); setMaintMsg("");
    try {
      const r = await talkMaintainNow(slug);
      if (!r.ok) setMaintMsg(r.reason || "Couldn't run the pass — try again.");
      else if (r.changed) setMaintMsg(`✓ Updated the article${r.resolved ? ` · resolved ${r.resolved}` : ""}.`);
      else setMaintMsg(`Ran the pass — no change needed${r.kept_open ? ` · ${r.kept_open} still open` : ""}.`);
      load();
    } catch (e: any) { setMaintMsg(e?.message || "Couldn't run the pass."); }
    finally { setMaintaining(false); }
  }

  const open = items.filter((t) => !t.resolved_at);
  const done = items.filter((t) => t.resolved_at);
  // Keep the actionable items (owner directives, conflicts/questions/todos) up top; fold
  // the inert informational 'note'/'decision' logs behind a summary so they never crowd them.
  const PRIORITY = new Set(["correction", "directive", "conflict", "question", "todo"]);
  const primary = open.filter((t) => PRIORITY.has(t.kind) || t.author === "user");
  const minor = open.filter((t) => !PRIORITY.has(t.kind) && t.author !== "user");

  function Replies({ t }: { t: Talk }) {
    if (!t.replies?.length) return null;
    return (
      <div className="talk-replies">
        {t.replies.map((r) => (
          <div key={r.id} className="talk-reply">
            <span className="talk-reply-who">{r.author === "ai" ? "AI" : "You"}:</span>{" "}
            <span>{r.body}</span>
          </div>
        ))}
      </div>
    );
  }

  function ItemActions({ t }: { t: Talk }) {
    return (
      <div className="talk-actions">
        <button className="link-btn" onClick={() => { setReplyFor(replyFor === t.id ? null : t.id); setReplyText(""); }}>
          {replyFor === t.id ? "Cancel" : "Reply"}
        </button>
        {t.kind !== "correction" && (
          <button className="link-btn" disabled={busy} onClick={() => dismiss(t)}>Dismiss</button>
        )}
      </div>
    );
  }

  function ReplyBox({ id }: { id: number }) {
    if (replyFor !== id) return null;
    return (
      <div className="talk-reply-add">
        <input className="modal-input" autoFocus placeholder="Reply — read by the next maintenance pass…"
               value={replyText} onChange={(e) => setReplyText(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && sendReply(id)} />
        <button className="ghost" disabled={busy || !replyText.trim()} onClick={() => sendReply(id)}>Send</button>
      </div>
    );
  }

  return (
    <div style={{ marginTop: 20 }}>
      <h3 style={{ marginBottom: 4 }}>AI talk</h3>
      <p className="muted" style={{ fontSize: 11, marginTop: 0 }}>
        The AI's reasoning for this article. Add a directive or a <strong>Correction</strong> (a fact fix that
        becomes a source-of-truth note), <strong>Reply</strong> to steer an item, or <strong>Dismiss</strong> one
        you've handled. Replies and new items are worked by the scheduled maintenance pass — or run one now.
      </p>

      {primary.map((t) => (
        <div key={t.id} className="talk-item-block">
          <div className="talk-item">
            <span title={t.kind}>{KIND_ICON[t.kind] || "•"}</span>
            <span className="talk-text">
              {t.body}{t.author === "user" && <em className="muted"> — you</em>}<CorrectionBadge t={t} />
            </span>
          </div>
          <Replies t={t} />
          <ItemActions t={t} />
          <ReplyBox id={t.id} />
        </div>
      ))}
      {open.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No open items.</p>}

      {minor.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>
            {minor.length} note{minor.length > 1 ? "s" : ""}
          </summary>
          {minor.map((t) => (
            <div key={t.id} className="talk-item-block">
              <div className="talk-item muted">
                <span title={t.kind}>{KIND_ICON[t.kind] || "•"}</span>
                <span className="talk-text">{t.body}</span>
              </div>
              <Replies t={t} />
              <ItemActions t={t} />
              <ReplyBox id={t.id} />
            </div>
          ))}
        </details>
      )}

      {open.length > 0 && (
        <div className="talk-maint-row">
          <button className="ghost" disabled={maintaining || !llm.ready} title={!llm.ready ? llm.reason : undefined} onClick={maintainNow}>
            {maintaining ? "Running…" : "Resolve with AI now"}
          </button>
          {maintMsg && <span className="muted" style={{ fontSize: 11 }}>{maintMsg}</span>}
        </div>
      )}

      <div className="talk-add">
        <select className="modal-select" value={kind} onChange={(e) => setKind(e.target.value)}>
          {ADD_KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        <input className="modal-input"
               placeholder={kind === "correction" ? "State the correct fact (e.g. spelled Bjørn, not Bjorn)…" : "Add a note for the next pass…"}
               value={body}
               onChange={(e) => setBody(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} />
        <button className="ghost" onClick={add}>Add</button>
      </div>
      {flash && <p className="muted" style={{ fontSize: 11, color: "#2a6a2a", marginTop: 4 }}>{flash}</p>}

      {done.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>{done.length} resolved</summary>
          {done.map((t) => (
            <div key={t.id} className="talk-item-block">
              <div className="talk-item muted">
                <span>{KIND_ICON[t.kind] || "•"}</span>
                <span className="talk-text">
                  <span style={{ textDecoration: "line-through" }}>{t.body}</span>
                  {t.resolution && <em style={{ display: "block", textDecoration: "none" }}>→ {t.resolution}</em>}
                </span>
              </div>
              <Replies t={t} />
            </div>
          ))}
        </details>
      )}
    </div>
  );
}
