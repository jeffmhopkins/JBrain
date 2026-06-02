import { useEffect, useState } from "react";
import Modal from "./Modal";
import {
  post, researchActivate, researchApprove, researchDetail, researchDismiss, researchRemove,
} from "../api";

interface RLink {
  id: number; label: string | null; url: string; spec_status: string;
  approved_count: number; sessions: number; reply_count: number; max_total_replies: number;
}

// Owner management for scoped Q&A "research" links: approve which notes are
// exposed, activate, audit sessions, revoke. (Creation is via the assistant.)
export default function ResearchLinks({ links, reload }: { links: RLink[]; reload: () => void }) {
  const [manage, setManage] = useState<number | null>(null);
  const [copied, setCopied] = useState<number | null>(null);

  function copy(l: RLink) {
    navigator.clipboard?.writeText(l.url).then(() => { setCopied(l.id); setTimeout(() => setCopied(null), 1200); });
  }
  async function revoke(l: RLink) {
    const draft = l.spec_status !== "active";
    const verb = draft ? "Delete this draft" : "Revoke";
    if (!confirm(`${verb} “${l.label || "research link"}”? It stops answering immediately.`)) return;
    try { await post(`/api/shares/${l.id}/revoke`); } finally { reload(); }
  }
  async function activate(l: RLink) {
    try { await researchActivate(l.id); } catch (e: any) { alert(e?.message || "Couldn't activate."); }
    reload();
  }

  return (
    <div style={{ marginTop: 24 }}>
      <div className="adv-section">Research links</div>
      <p className="muted" style={{ fontSize: 13 }}>
        Scoped, read-only Q&amp;A links — a recipient asks an AI that can only read the notes you approve.
        Create one by asking the assistant (e.g. “create a research link of my medical history”), then
        approve &amp; activate it here.
      </p>
      {links.length === 0 && <p className="muted" style={{ fontSize: 13 }}>None yet — ask the assistant to create one.</p>}
      {links.map((l) => {
        const draft = l.spec_status !== "active";
        return (
          <div className="card" key={l.id}>
            <div className="row">
              <strong>{l.label || "Research link"}</strong>
              {draft ? <span className="badge tag-delete">draft — not live</span> : <span className="badge prio">live</span>}
              <span className="badge">{l.approved_count} note{l.approved_count === 1 ? "" : "s"}</span>
              {l.sessions > 0 && <span className="badge">{l.sessions} chat{l.sessions === 1 ? "" : "s"}</span>}
              <span className="spacer" />
              <span className="muted" style={{ fontSize: 12 }}>{l.reply_count}/{l.max_total_replies} answers used</span>
            </div>
            {!draft && (
              <div className="row" style={{ marginTop: 6, gap: 6 }}>
                <input readOnly value={l.url} onFocus={(e) => e.currentTarget.select()} style={{ fontSize: 12 }} />
                <button className="ghost" onClick={() => copy(l)}>{copied === l.id ? "Copied" : "Copy"}</button>
                <button className="ghost" onClick={() => setManage(l.id)}>Manage</button>
                <button className="ghost danger-hover" onClick={() => revoke(l)}>Revoke</button>
              </div>
            )}
            {draft && (
              <div className="row" style={{ marginTop: 8, gap: 8 }}>
                <button className="primary" disabled={l.approved_count === 0}
                        title={l.approved_count === 0 ? "Approve at least one note first (Manage)" : ""}
                        onClick={() => activate(l)}>Activate link</button>
                <button className="ghost" onClick={() => setManage(l.id)}>
                  Manage{l.approved_count === 0 ? " — approve notes" : ""}
                </button>
                <button className="ghost danger-hover" onClick={() => revoke(l)}>Delete</button>
              </div>
            )}
          </div>
        );
      })}
      {manage != null && <ManageModal linkId={manage} onClose={() => { setManage(null); reload(); }} />}
    </div>
  );
}

function ManageModal({ linkId, onClose }: { linkId: number; onClose: () => void }) {
  const [d, setD] = useState<any>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() { try { setD(await researchDetail(linkId)); } catch { /* ignore */ } }
  useEffect(() => { refresh(); }, [linkId]);

  async function act(fn: () => Promise<any>) { setBusy(true); try { await fn(); await refresh(); } finally { setBusy(false); } }

  if (!d) return <Modal title="Research link" onClose={onClose} footer={null}><p className="muted">Loading…</p></Modal>;

  const draft = d.spec.status === "draft";
  return (
    <Modal title={`Research link${draft ? " (draft)" : ""}`} size="wide" onClose={onClose} footer={<>
      {draft && <button className="primary" disabled={busy || d.approved.length === 0}
                        onClick={() => act(() => researchActivate(linkId)).then(onClose)}>Activate</button>}
      <button className="ghost" onClick={onClose}>Close</button>
    </>}>
      <div className="adv-section">Approved — the link can read these ({d.approved.length})</div>
      {d.approved.length === 0 && <p className="muted" style={{ fontSize: 13 }}>Nothing approved yet. Approve candidates below.</p>}
      {d.approved.map((n: any) => (
        <div className="row" key={n.id} style={{ gap: 8, padding: "3px 0" }}>
          <span style={{ fontSize: 13 }}>{n.title}</span>
          <span className="spacer" />
          <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }}
                  disabled={busy} onClick={() => act(() => researchRemove(linkId, [n.id]))}>Remove</button>
        </div>
      ))}

      <div className="adv-section" style={{ marginTop: 16 }}>
        Pending candidates ({d.candidates.length})
        {d.candidates.length > 0 && (
          <button className="ghost" style={{ fontSize: 11, padding: "2px 8px", marginLeft: 8 }} disabled={busy}
                  onClick={() => act(() => researchApprove(linkId, d.candidates.map((c: any) => c.id)))}>Approve all</button>
        )}
      </div>
      {d.candidates.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No new candidates.</p>}
      {d.candidates.map((n: any) => (
        <div className="row" key={n.id} style={{ gap: 8, padding: "3px 0" }}>
          <span style={{ fontSize: 13 }}>{n.title}</span>
          <span className="spacer" />
          <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }}
                  disabled={busy} onClick={() => act(() => researchApprove(linkId, [n.id]))}>Approve</button>
          <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }}
                  disabled={busy} onClick={() => act(() => researchDismiss(linkId, [n.id]))}>Dismiss</button>
        </div>
      ))}

      {d.sessions.length > 0 && <>
        <div className="adv-section" style={{ marginTop: 16 }}>Sessions ({d.sessions.length})</div>
        {d.sessions.map((s: any) => (
          <div className="muted" key={s.id} style={{ fontSize: 12 }}>
            {s.name || "Someone"} · {s.turn_count} question(s) · {s.retrieved} note(s) used
            {s.denied_count ? ` · ${s.denied_count} blocked` : ""}
          </div>
        ))}
      </>}
    </Modal>
  );
}
