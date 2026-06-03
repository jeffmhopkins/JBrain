import { useEffect, useState } from "react";
import { get, post, del } from "../api";
import { fmtTs } from "../time";
import { useAuth } from "../App";

interface Item {
  id: number; source: string; content: string;
  lat: number | null; lon: number | null; location_label: string | null;
  processed: number; created_at: string;
}

// Quick-capture inbox: dictated/captured snippets land here and were previously only
// reachable by the AI. This lets the owner see, file (mark done), or delete them.
export default function InboxPage() {
  const { appTz } = useAuth();
  const [items, setItems] = useState<Item[]>([]);
  const [showDone, setShowDone] = useState(false);

  const load = () => get<Item[]>(`/api/capture?include_processed=${showDone}`).then(setItems).catch(() => setItems([]));
  useEffect(() => { load(); }, [showDone]);

  async function done(it: Item, processed: boolean) {
    setItems((xs) => xs.filter((x) => x.id !== it.id || showDone));
    try { await post(`/api/capture/${it.id}/processed?processed=${processed}`); } catch { /* ignore */ } finally { load(); }
  }
  async function remove(it: Item) {
    if (!confirm("Delete this captured item?")) return;
    setItems((xs) => xs.filter((x) => x.id !== it.id));
    try { await del(`/api/capture/${it.id}`); } catch { /* ignore */ } finally { load(); }
  }

  return (
    <div className="tool-body">
      <div className="row" style={{ alignItems: "center", marginBottom: 8 }}>
        <h2 style={{ margin: 0 }}>Inbox</h2>
        <span className="spacer" />
        <label style={{ fontSize: 13, display: "inline-flex", gap: 6, alignItems: "center", cursor: "pointer" }}>
          <input type="checkbox" checked={showDone} onChange={() => setShowDone((s) => !s)} /> show filed
        </label>
      </div>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Quick-captured snippets. The assistant files these into notes; you can also mark them done or delete them here.
      </p>
      {items.length === 0 ? (
        <p className="muted">Nothing in the inbox.</p>
      ) : (
        <ul className="inbox-list">
          {items.map((it) => (
            <li key={it.id} className={"inbox-item" + (it.processed ? " done" : "")}>
              <div className="inbox-content">{it.content}</div>
              <div className="muted" style={{ fontSize: 12 }}>
                {fmtTs(it.created_at, appTz)} · {it.source}
                {it.location_label ? ` · ${it.location_label}` : ""}
                {it.processed ? " · filed" : ""}
              </div>
              <div className="row" style={{ gap: 8, marginTop: 6 }}>
                {!it.processed
                  ? <button className="ghost" onClick={() => done(it, true)}>Mark filed</button>
                  : <button className="ghost" onClick={() => done(it, false)}>Restore</button>}
                <button className="place-del" title="Delete" onClick={() => remove(it)}>✕</button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
