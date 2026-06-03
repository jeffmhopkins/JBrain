import { useEffect, useState } from "react";
import Modal from "./Modal";
import {
  post, researchActivate, researchApprove, researchDetail, researchDismiss, researchRemove,
  researchResetBind, researchSetDetails,
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
      <p className="share-hint">Read-only Q&amp;A scoped to notes you approve — create one via the assistant.</p>
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

interface Settings { persona_voice: string; topics: string; intro: string; bind: boolean; single_use: boolean; ttl_days: number; }

function ManageModal({ linkId, onClose }: { linkId: number; onClose: () => void }) {
  const [d, setD] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [s, setS] = useState<Settings | null>(null);
  const [saved, setSaved] = useState(false);

  async function refresh() {
    try {
      const detail = await researchDetail(linkId);
      setD(detail);
      // Load the CURRENT remaining days so saving other settings doesn't wipe the expiry.
      const remaining = detail.expires_at
        ? Math.max(0, Math.ceil((Date.parse(detail.expires_at.replace(" ", "T") + "Z") - Date.now()) / 86400000))
        : 0;
      setS({
        persona_voice: detail.spec.persona_voice || "", topics: detail.spec.topics || "",
        intro: detail.spec.intro || "", bind: !!detail.spec.bind, single_use: !!detail.spec.single_use,
        ttl_days: remaining,
      });
    } catch { /* ignore */ }
  }
  useEffect(() => { refresh(); }, [linkId]);

  async function act(fn: () => Promise<any>) { setBusy(true); try { await fn(); await refresh(); } finally { setBusy(false); } }
  async function saveSettings() {
    if (!s || !d) return;
    setBusy(true);
    try {
      // Carry the caps so the details endpoint (which defaults them) doesn't reset them.
      await researchSetDetails(linkId, { ...s, max_turns: d.spec.max_turns, max_total_replies: d.spec.max_total_replies });
      setSaved(true); setTimeout(() => setSaved(false), 1500); await refresh();
    } catch (e: any) { alert(e?.message || "Couldn't save."); }
    finally { setBusy(false); }
  }
  const set = (patch: Partial<Settings>) => setS((p) => p ? { ...p, ...patch } : p);

  if (!d || !s) return <Modal title="Research link" onClose={onClose} footer={null}><p className="muted">Loading…</p></Modal>;

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

      <div className="adv-section" style={{ marginTop: 16 }}>Settings</div>
      <label className="share-field">What the AI may &amp; may NOT discuss (scope)
        <textarea rows={2} value={s.topics} disabled={busy}
                  placeholder="e.g. only medications and allergies; never finances or family"
                  onChange={(e) => set({ topics: e.target.value })} /></label>
      <label className="share-field">Tone / role (persona)
        <textarea rows={2} value={s.persona_voice} disabled={busy}
                  placeholder="e.g. answer warmly, like a helpful nurse"
                  onChange={(e) => set({ persona_voice: e.target.value })} /></label>
      <label className="share-field">Recipient greeting (intro)
        <textarea rows={2} value={s.intro} disabled={busy}
                  onChange={(e) => set({ intro: e.target.value })} /></label>
      <div className="row" style={{ gap: 14, flexWrap: "wrap", marginTop: 4 }}>
        <label className="row" style={{ gap: 6 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={s.bind} disabled={busy}
                 onChange={(e) => set({ bind: e.target.checked })} /> Lock to first browser</label>
        <label className="row" style={{ gap: 6 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={s.single_use} disabled={busy}
                 onChange={(e) => set({ single_use: e.target.checked })} /> Single use</label>
        <label className="row" style={{ gap: 6 }}>Expires in
          <input type="number" min={0} style={{ width: 64 }} value={s.ttl_days} disabled={busy}
                 onChange={(e) => set({ ttl_days: Math.max(0, +e.target.value) })} /> days (0 = never)</label>
      </div>
      <p className="muted" style={{ fontSize: 12, margin: "4px 0" }}>
        {d.expires_at ? `Currently expires ${d.expires_at} UTC.` : "Currently never expires."}
        {d.bound ? " · Locked to a device." : ""}
      </p>
      <div className="row" style={{ gap: 8, marginTop: 6 }}>
        <button className="primary" disabled={busy} onClick={saveSettings}>{saved ? "Saved ✓" : "Save settings"}</button>
        {d.bound && <button className="ghost" disabled={busy} onClick={() => act(() => researchResetBind(linkId))}>Reset device lock</button>}
      </div>

      {d.sessions.length > 0 && <>
        <div className="adv-section" style={{ marginTop: 16 }}>Sessions ({d.sessions.length})</div>
        {d.sessions.map((se: any) => (
          <div className="muted" key={se.id} style={{ fontSize: 12 }}>
            {se.name || "Someone"} · {se.turn_count} question(s) · {se.retrieved} note(s) used
            {se.denied_count ? ` · ${se.denied_count} blocked` : ""}
          </div>
        ))}
      </>}
    </Modal>
  );
}
