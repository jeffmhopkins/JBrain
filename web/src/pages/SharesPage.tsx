import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get, post } from "../api";

interface ShareLink { id: number; token: string; scope: "view" | "edit"; label: string | null; created_at: string; last_used_at: string | null; expires_at: string | null; pending: number; note_title: string; note_slug: string; url: string; }
interface Proposal { id: number; note_title: string; note_slug: string; proposed_content: string; current_content: string; proposer_name: string | null; proposer_note: string | null; created_at: string; stale: boolean; }
interface HistItem { id: number; proposer_name: string | null; status: string; created_at: string; resolved_at: string | null; note_title: string; note_slug: string; }

const leaf = (t: string) => t.replace(/^(notes|kb|lists)\//i, "");
const STATUS_CLR: Record<string, string> = { accepted: "#4ade80", rejected: "var(--danger)", superseded: "var(--text-dim)" };

// Cheap line-level diff (set difference of non-empty trimmed lines) so the owner
// can see what an editor added/removed — the guard against a list-wipe proposal.
function diff(cur: string, prop: string) {
  const a = new Set((cur || "").split("\n").map((s) => s.trim()).filter(Boolean));
  const b = new Set((prop || "").split("\n").map((s) => s.trim()).filter(Boolean));
  return { removed: [...a].filter((x) => !b.has(x)), added: [...b].filter((x) => !a.has(x)) };
}

export default function SharesPage() {
  const [links, setLinks] = useState<ShareLink[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [history, setHistory] = useState<HistItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState<number | null>(null);
  const [openDiff, setOpenDiff] = useState<number | null>(null);

  async function load() {
    setLoading(true);
    try {
      const r = await get<{ links: ShareLink[]; proposals: Proposal[]; history: HistItem[] }>("/api/shares");
      setLinks(r.links); setProposals(r.proposals); setHistory(r.history || []);
    } catch { /* ignore */ }
    setLoading(false);
  }
  useEffect(() => { load(); }, []);

  async function copy(l: ShareLink) {
    try { await navigator.clipboard.writeText(l.url); setCopied(l.id); setTimeout(() => setCopied(null), 1500); }
    catch { prompt("Copy this link:", l.url); }
  }
  async function revoke(l: ShareLink) {
    if (!confirm(`Revoke this ${l.scope} link for “${leaf(l.note_title)}”? It stops working immediately.`)) return;
    setLinks((ls) => ls.filter((x) => x.id !== l.id));
    try { await post(`/api/shares/${l.id}/revoke`); } catch { load(); }
  }
  async function accept(p: Proposal) {
    try { await post(`/api/shares/proposals/${p.id}/accept`); load(); }
    catch (e: any) { alert(e?.message || "Couldn't accept — the note may have changed since this was proposed."); load(); }
  }
  async function reject(p: Proposal) {
    setProposals((ps) => ps.filter((x) => x.id !== p.id));
    try { await post(`/api/shares/proposals/${p.id}/reject`); load(); } catch { load(); }
  }

  return (
    <div className="content">
      <h2 style={{ marginTop: 0 }}>Shares</h2>
      {loading && <p className="muted">Loading…</p>}

      {proposals.length > 0 && (
        <>
          <div className="adv-section" style={{ marginTop: 0 }}>Pending proposals</div>
          {proposals.map((p) => {
            const d = diff(p.current_content, p.proposed_content);
            return (
              <div className="card" key={p.id}>
                <div className="row">
                  <strong>{leaf(p.note_title)}</strong>
                  <span className="badge">{p.proposer_name ? `${p.proposer_name} proposed` : "edit proposed"}</span>
                  {p.stale && <span className="badge tag-delete" title="The note changed since this was proposed">stale</span>}
                  <span className="spacer" />
                  <Link className="ghost" to={`/note/${p.note_slug}`} style={{ fontSize: 13, padding: "4px 8px" }}>Open note</Link>
                </div>
                {p.proposer_note && <div className="muted" style={{ fontSize: 13, margin: "4px 0" }}>“{p.proposer_note}”</div>}
                <div className="share-diff">
                  {d.removed.length === 0 && d.added.length === 0 && <span className="muted">No textual change.</span>}
                  {d.removed.map((l, i) => <div key={"r" + i} style={{ color: "var(--danger)" }}>− {l}</div>)}
                  {d.added.map((l, i) => <div key={"a" + i} style={{ color: "#4ade80" }}>+ {l}</div>)}
                </div>
                <button className="ghost" style={{ fontSize: 12 }} onClick={() => setOpenDiff(openDiff === p.id ? null : p.id)}>
                  {openDiff === p.id ? "Hide" : "Show"} full proposed content
                </button>
                {openDiff === p.id && <pre className="share-diff" style={{ marginTop: 6 }}>{p.proposed_content}</pre>}
                <div className="row" style={{ marginTop: 8, gap: 8 }}>
                  <button className="primary" onClick={() => accept(p)}>Accept</button>
                  <button className="ghost" onClick={() => reject(p)}>Reject</button>
                </div>
              </div>
            );
          })}
        </>
      )}

      <div className="adv-section">Active links</div>
      {!loading && links.length === 0 && (
        <p className="muted">No active share links. Open a note and mint one, or ask the assistant to “share [[X]] as a view link.”</p>
      )}
      {links.map((l) => (
        <div className="card" key={l.id}>
          <div className="row">
            <strong>{leaf(l.note_title)}</strong>
            <span className={"badge " + (l.scope === "edit" ? "badge-architect" : "")}>{l.scope}</span>
            {l.label && <span className="badge">{l.label}</span>}
            {l.expires_at && <span className="badge" title="Link expiry">expires {l.expires_at.slice(0, 10)}</span>}
            {l.pending > 0 && <span className="badge tag-delete">{l.pending} pending</span>}
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 12 }}>{l.last_used_at ? `viewed ${l.last_used_at.replace(/\.\d+$/, "")}` : "not viewed yet"}</span>
          </div>
          <div className="row" style={{ marginTop: 6, gap: 6 }}>
            <input readOnly value={l.url} onFocus={(e) => e.currentTarget.select()} style={{ fontSize: 12 }} />
            <button className="ghost" onClick={() => copy(l)}>{copied === l.id ? "Copied" : "Copy"}</button>
            <button className="ghost" onClick={() => revoke(l)}>Revoke</button>
          </div>
        </div>
      ))}

      {history.length > 0 && (
        <>
          <div className="adv-section">History</div>
          <div className="card">
            {history.map((h) => (
              <div key={h.id} className="row" style={{ padding: "5px 0", fontSize: 13, gap: 8 }}>
                <span style={{ color: STATUS_CLR[h.status] || "var(--text-dim)", textTransform: "capitalize", minWidth: 80 }}>{h.status}</span>
                <Link to={`/note/${h.note_slug}`} className="wikilink">{leaf(h.note_title)}</Link>
                <span className="muted">{h.proposer_name || "someone"}</span>
                <span className="spacer" />
                <span className="muted" style={{ fontSize: 12 }}>{(h.resolved_at || h.created_at).replace(/\.\d+$/, "")}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
