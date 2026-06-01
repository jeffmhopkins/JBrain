import { useEffect, useState } from "react";
import { get, post } from "../api";

interface Action {
  id: number;
  type: "CREATE" | "UPDATE" | "LINK" | "RENAME";
  payload: any;
}

export default function StagingPanel({ tick, onChange }: { tick: number; onChange: () => void }) {
  const [actions, setActions] = useState<Action[]>([]);

  async function load() {
    try { setActions(await get("/api/staging")); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, [tick]);

  async function apply(id: number) { await post(`/api/staging/${id}/apply`); onChange(); }
  async function reject(id: number) { await post(`/api/staging/${id}/reject`); onChange(); }
  async function applyAll() { await post("/api/staging/apply-all"); onChange(); }

  if (actions.length === 0) {
    return <p className="muted" style={{ fontSize: 13 }}>No pending proposals.</p>;
  }

  return (
    <div>
      <div className="row" style={{ margin: "8px 0" }}>
        <span className="badge">{actions.length} pending</span>
        <div className="spacer" />
        <button className="primary" onClick={applyAll}>Apply all</button>
      </div>
      {actions.map((a) => {
        const cls = a.type === "CREATE" ? "tag-create" : a.type === "UPDATE" ? "tag-update"
          : a.type === "RENAME" ? "tag-update" : "tag-link";
        const title = a.type === "RENAME"
          ? `${a.payload.title} → ${a.payload.new_title}`
          : a.payload.title || `${a.payload.source_title} → ${a.payload.target_title}`;
        return (
          <div className="card" key={a.id}>
            <div className="row">
              <strong className={cls}>{a.type}</strong>
              <span className="spacer" />
            </div>
            <div style={{ fontWeight: 600, margin: "4px 0" }}>{title}</div>
            <div className="muted" style={{ fontSize: 13 }}>{a.payload.summary}</div>
            <div className="row" style={{ marginTop: 10 }}>
              <button className="primary" onClick={() => apply(a.id)}>Apply</button>
              <button className="ghost" onClick={() => reject(a.id)}>Reject</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
