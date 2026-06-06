import { useEffect, useState } from "react";
import { get, post } from "../api";
import ApprovalView, { Preview, previewStat } from "./ApprovalView";

interface Action { id: number; type: string; payload: any; preview?: Preview | null; warnings?: string[]; }

const TAG_CLASS: Record<string, string> = {
  CREATE: "tag-create", UPDATE: "tag-update", LINK: "tag-link", RENAME: "tag-update",
  LIST_EDIT_ITEM: "tag-update", EDIT_PLACE: "tag-update", SET_TAGS: "tag-update",
  DELETE: "tag-delete", DELETE_LIST: "tag-delete", LIST_REMOVE_ITEM: "tag-delete",
  ADD_PLACE: "tag-create",
};

function actionTitle(a: Action): string {
  if (a.preview?.label) return a.preview.label;
  const p = a.payload;
  return p.title || p.name || p.list_title || p.source_title || "";
}

export default function StagingPanel({ tick, onChange }: { tick: number; onChange: () => void }) {
  const [actions, setActions] = useState<Action[]>([]);
  const [open, setOpen] = useState<number | null>(null);   // inline expand
  const [modal, setModal] = useState<Action | null>(null); // full-screen review

  async function load() {
    try { setActions(await get("/api/staging")); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, [tick]);

  async function run(fn: () => Promise<unknown>, what: string) {
    try { await fn(); }
    catch (e: any) { alert(e?.message || `Couldn't ${what}.`); }
    finally { setModal(null); onChange(); load(); }
  }
  const apply = (id: number) => run(() => post(`/api/staging/${id}/apply`), "apply this change");
  const reject = (id: number) => run(() => post(`/api/staging/${id}/reject`), "reject this change");
  const applyAll = () => run(() => post("/api/staging/apply-all"), "apply all");

  if (actions.length === 0) {
    return null;   // nothing staged → show nothing, not an empty-state line
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
        const pv = a.preview;
        const stat = previewStat(pv);
        return (
          <div className="card" key={a.id}>
            <div className="row" style={{ alignItems: "center", gap: 8 }}>
              <strong className={cls}>{a.type}</strong>
              <span style={{ fontWeight: 600 }}>{actionTitle(a)}</span>
              {pv?.stale && <span className="badge tag-delete" title="The note changed since this was proposed — applying will be refused; re-propose.">stale</span>}
              {pv?.conflict && <span className="badge tag-delete" title={pv.conflict}>conflict</span>}
              <span className="spacer" />
              {stat && <span className="muted" style={{ fontSize: 12 }}>{stat}</span>}
            </div>
            {a.payload.summary && <div className="muted" style={{ fontSize: 13, margin: "2px 0" }}>{a.payload.summary}</div>}
            {a.warnings?.length ? (
              <div className="share-banner" style={{ marginTop: 6, fontSize: 12, color: "var(--danger)" }}>
                ⚠ {a.warnings.map((w, i) => <div key={i}>{w}</div>)}
              </div>
            ) : null}
            {pv && (
              <button className="ghost" style={{ fontSize: 12, marginTop: 4 }}
                      onClick={() => setOpen(open === a.id ? null : a.id)}>
                {open === a.id ? "Hide" : "See"} changes
              </button>
            )}
            {pv && <button className="ghost" style={{ fontSize: 12, marginTop: 4, marginLeft: 8 }} onClick={() => setModal(a)}>Review ⤢</button>}
            {pv && open === a.id && <div style={{ marginTop: 6 }}><ApprovalView preview={pv} /></div>}
            {pv?.conflict && <div className="muted" style={{ fontSize: 12, color: "var(--danger)", marginTop: 4 }}>{pv.conflict}</div>}
            <div className="row" style={{ marginTop: 10 }}>
              <button className="primary" onClick={() => apply(a.id)}>Apply</button>
              <button className="ghost" onClick={() => reject(a.id)}>Reject</button>
            </div>
          </div>
        );
      })}

      {modal && (
        <div className="modal-backdrop" onClick={() => setModal(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <strong className={TAG_CLASS[modal.type] || "tag-link"}>{modal.type}</strong>
              <span className="modal-title" style={{ marginLeft: 8 }}>{actionTitle(modal)}</span>
              {modal.preview?.stale && <span className="badge tag-delete" style={{ marginLeft: 8 }}>stale</span>}
              <span className="spacer" />
              <button className="icon-btn" onClick={() => setModal(null)} aria-label="Close">✕</button>
            </div>
            <div className="modal-body">
              {modal.warnings?.length ? (
                <div className="share-banner" style={{ marginBottom: 8, fontSize: 12, color: "var(--danger)" }}>
                  ⚠ {modal.warnings.map((w, i) => <div key={i}>{w}</div>)}
                </div>
              ) : null}
              {modal.preview ? <ApprovalView preview={modal.preview} full /> : <span className="muted">No preview.</span>}
            </div>
            <div className="modal-foot">
              <span className="spacer" />
              <button className="ghost" onClick={() => reject(modal.id)}>Reject</button>
              <button className="primary" onClick={() => apply(modal.id)}>Apply</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
