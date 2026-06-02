import { useEffect, useState } from "react";
import Modal from "./Modal";
import {
  post, researchActivate, researchApprove, researchDetail, researchDismiss,
  researchMint, researchRemove,
} from "../api";

interface RLink {
  id: number; label: string | null; url: string; spec_status: string;
  approved_count: number; sessions: number; reply_count: number; max_total_replies: number;
}

// Owner management for scoped Q&A "research" links: mint (draft), approve which
// notes are exposed, activate, audit sessions, revoke.
export default function ResearchLinks({ links, reload }: { links: RLink[]; reload: () => void }) {
  const [minting, setMinting] = useState(false);
  const [manage, setManage] = useState<number | null>(null);
  const [copied, setCopied] = useState<number | null>(null);

  function copy(l: RLink) {
    navigator.clipboard?.writeText(l.url).then(() => { setCopied(l.id); setTimeout(() => setCopied(null), 1200); });
  }
  async function revoke(l: RLink) {
    if (!confirm(`Revoke “${l.label || "research link"}”? It stops answering immediately.`)) return;
    try { await post(`/api/shares/${l.id}/revoke`); } finally { reload(); }
  }

  return (
    <div style={{ marginTop: 24 }}>
      <div className="row">
        <h3 style={{ margin: 0 }}>Research links</h3>
        <span className="spacer" />
        <button className="ghost" onClick={() => setMinting(true)}>+ New research link</button>
      </div>
      <p className="muted" style={{ fontSize: 13 }}>
        Scoped, read-only Q&amp;A links — a recipient asks an AI that can only read the notes you approve.
      </p>
      {links.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No research links.</p>}
      {links.map((l) => (
        <div className="card" key={l.id}>
          <div className="row">
            <strong>{l.label || "Research link"}</strong>
            <span className={"badge badge-" + (l.spec_status === "active" ? "architect" : "import")}>{l.spec_status}</span>
            <span className="spacer" />
            <span className="badge">{l.approved_count} notes</span>
            <span className="badge">{l.sessions} chats</span>
          </div>
          <div className="muted" style={{ fontSize: 12, margin: "4px 0" }}>
            {l.reply_count}/{l.max_total_replies} answers used
            {l.spec_status === "draft" && " · not live yet — approve notes & activate"}
          </div>
          <div className="wf-actions">
            <button className="ghost" onClick={() => copy(l)}>{copied === l.id ? "Copied!" : "Copy link"}</button>
            <button className="ghost" onClick={() => setManage(l.id)}>Manage</button>
            <button className="ghost danger-hover" onClick={() => revoke(l)}>Revoke</button>
          </div>
        </div>
      ))}
      {minting && <MintModal onClose={() => setMinting(false)}
                             onMinted={(id) => { setMinting(false); setManage(id); reload(); }} />}
      {manage != null && <ManageModal linkId={manage} onClose={() => { setManage(null); reload(); }} />}
    </div>
  );
}

function MintModal({ onClose, onMinted }: { onClose: () => void; onMinted: (id: number) => void }) {
  const [label, setLabel] = useState("");
  const [prefixes, setPrefixes] = useState("");
  const [voice, setVoice] = useState("");
  const [intro, setIntro] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function mint() {
    const pre = prefixes.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
    if (pre.length === 0) { setErr("Add at least one folder, e.g. notes/Medical"); return; }
    setBusy(true); setErr("");
    try {
      const r = await researchMint({ label: label.trim() || undefined, prefixes: pre,
                                     persona_voice: voice.trim(), intro: intro.trim() });
      onMinted(r.link_id);
    } catch (e: any) { setErr(e?.message || "Couldn't create the link."); }
    finally { setBusy(false); }
  }

  return (
    <Modal title="New research link" onClose={onClose} footer={<>
      <button className="primary" disabled={busy} onClick={mint}>Create draft</button>
      <button className="ghost" onClick={onClose}>Cancel</button>
      {err && <><span className="spacer" /><span style={{ color: "var(--danger)", fontSize: 13 }}>{err}</span></>}
    </>}>
      <label className="muted">Label</label>
      <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Medical history" />
      <label className="muted" style={{ marginTop: 12, display: "block" }}>Folders to draw from (one per line)</label>
      <textarea className="wf-textarea-lg" value={prefixes} onChange={(e) => setPrefixes(e.target.value)}
                placeholder={"notes/Medical"} />
      <p className="muted" style={{ fontSize: 12 }}>
        These only <em>find</em> candidate notes — you approve exactly which ones the link can read in the next step.
      </p>
      <label className="muted" style={{ marginTop: 8, display: "block" }}>Persona / tone (optional)</label>
      <input value={voice} onChange={(e) => setVoice(e.target.value)} placeholder="e.g. warm and concise" />
      <label className="muted" style={{ marginTop: 12, display: "block" }}>Intro shown to the recipient (optional)</label>
      <input value={intro} onChange={(e) => setIntro(e.target.value)} placeholder="Ask me about my medical history…" />
    </Modal>
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
