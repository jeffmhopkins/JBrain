import { useState } from "react";
import { get, post } from "../api";

// Owner management for lab-share links (the PHI surface): list, copy, AUDIT who opened them +
// which results were shown, and REVOKE. Mirrors ResearchLinks. Creation is on the Labs page.

interface LabLink {
  id: number; label: string | null; url: string; created_at: string; expires_at: string | null;
  allow_chat: number; analyte_count: number; sessions: number; reply_count: number; max_total_replies: number;
}
interface AuditSession {
  id: number; name: string | null; status: string; turns: number; charted: string[];
  denied: number; created_at: string; last_at: string | null;
}

export default function LabShareLinks({ links, reload }: { links: LabLink[]; reload: () => void }) {
  const [copied, setCopied] = useState<number | null>(null);
  const [openAudit, setOpenAudit] = useState<number | null>(null);
  const [audit, setAudit] = useState<Record<number, AuditSession[]>>({});

  function copy(l: LabLink) {
    navigator.clipboard?.writeText(l.url).then(() => { setCopied(l.id); setTimeout(() => setCopied(null), 1200); });
  }
  async function revoke(l: LabLink) {
    if (!confirm(`Revoke “${l.label || "lab results link"}”? It stops working immediately (already-downloaded charts can't be recalled).`)) return;
    try { await post(`/api/shares/${l.id}/revoke`); } finally { reload(); }
  }
  async function toggleAudit(l: LabLink) {
    if (openAudit === l.id) { setOpenAudit(null); return; }
    setOpenAudit(l.id);
    if (!audit[l.id]) {
      try { const r = await get<{ sessions: AuditSession[] }>(`/api/shares/labs/${l.id}/audit`);
            setAudit((a) => ({ ...a, [l.id]: r.sessions })); } catch { /* ignore */ }
    }
  }

  if (!links.length) return null;
  return (
    <div style={{ marginTop: 24 }}>
      <div className="adv-section">Lab result links</div>
      <p className="share-hint">Scoped trend charts you shared — locked to one device and expiring. Revoke any you didn’t mean to send.</p>
      {links.map((l) => (
        <div className="card" key={l.id}>
          <div className="row">
            <strong>{l.label || "Lab results link"}</strong>
            <span className="badge prio">live</span>
            <span className="badge">{l.analyte_count} result{l.analyte_count === 1 ? "" : "s"}</span>
            {!!l.allow_chat && <span className="badge">AI chat</span>}
            {l.sessions > 0 && <span className="badge">{l.sessions} view{l.sessions === 1 ? "" : "s"}</span>}
            <span className="spacer" />
            {l.expires_at && <span className="muted" style={{ fontSize: 12 }}>expires {l.expires_at.slice(0, 10)}</span>}
          </div>
          <div className="row" style={{ marginTop: 6, gap: 6 }}>
            <input className="share-url" readOnly value={l.url} onFocus={(e) => e.currentTarget.select()} />
            <button className="ghost" onClick={() => copy(l)}>{copied === l.id ? "Copied ✓" : "Copy"}</button>
            <button className="ghost" onClick={() => toggleAudit(l)}>
              {openAudit === l.id ? "Hide activity" : "Activity"}</button>
            <button className="ghost danger-hover" onClick={() => revoke(l)}>Revoke</button>
          </div>
          {openAudit === l.id && (
            <div style={{ marginTop: 8 }}>
              {(audit[l.id] || []).length === 0
                ? <p className="muted" style={{ fontSize: 12 }}>No one has opened this link yet.</p>
                : (audit[l.id] || []).map((s) => (
                    <div key={s.id} className="applied-chip" style={{ fontSize: 12 }}>
                      <span>{s.name || "Someone"}</span>
                      <span className="muted">{(s.last_at || s.created_at)?.slice(0, 16).replace("T", " ")}</span>
                      <span className="spacer" />
                      <span className="muted">{s.charted.length} chart{s.charted.length === 1 ? "" : "s"}
                        {s.turns ? ` · ${s.turns} question${s.turns === 1 ? "" : "s"}` : ""}</span>
                    </div>
                  ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
