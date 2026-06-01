import { useEffect, useState } from "react";
import { get, post } from "../api";

type ActionType = "CREATE" | "UPDATE" | "LINK" | "RENAME" | "DELETE"
  | "LIST_REMOVE_ITEM" | "LIST_EDIT_ITEM" | "DELETE_LIST";
interface Action { id: number; type: ActionType; payload: any; }

const TAG_CLASS: Record<string, string> = {
  CREATE: "tag-create", UPDATE: "tag-update", LINK: "tag-link", RENAME: "tag-update",
  LIST_EDIT_ITEM: "tag-update", DELETE: "tag-delete", DELETE_LIST: "tag-delete", LIST_REMOVE_ITEM: "tag-delete",
};

function actionTitle(a: Action): string {
  const p = a.payload;
  switch (a.type) {
    case "RENAME": return `${p.title} → ${p.new_title}`;
    case "LINK": return `${p.source_title} → ${p.target_title}`;
    case "DELETE": return `Delete ${p.title}`;
    case "DELETE_LIST": return `Delete list ${p.list_title}`;
    case "LIST_REMOVE_ITEM": return `Remove “${p.item}” from ${p.list_title}`;
    case "LIST_EDIT_ITEM": return `“${p.item}” → “${p.new_item}”`;
    default: return p.title || "";
  }
}

export default function StagingPanel({ tick, onChange }: { tick: number; onChange: () => void }) {
  const [actions, setActions] = useState<Action[]>([]);

  async function load() {
    try { setActions(await get("/api/staging")); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, [tick]);

  // Always surface a failure (and refresh) — a swallowed error makes Apply look
  // like it did nothing.
  async function run(fn: () => Promise<unknown>, what: string) {
    try { await fn(); }
    catch (e: any) { alert(e?.message || `Couldn't ${what}.`); }
    finally { onChange(); load(); }
  }
  const apply = (id: number) => run(() => post(`/api/staging/${id}/apply`), "apply this change");
  const reject = (id: number) => run(() => post(`/api/staging/${id}/reject`), "reject this change");
  const applyAll = () => run(() => post("/api/staging/apply-all"), "apply all");

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
        const cls = TAG_CLASS[a.type] || "tag-link";
        const title = actionTitle(a);
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
