import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { get, guidedAccept, guidedActivate, guidedOptions, guidedReject, guidedResetBind, post } from "../api";
import { useAuth } from "../App";
import { fmtTs } from "../time";

interface ShareLink { id: number; token: string; scope: "view" | "edit"; label: string | null; created_at: string; last_used_at: string | null; expires_at: string | null; bind: number; bound_at: string | null; pending: number; note_title: string; note_slug: string; url: string; }
interface Proposal { id: number; note_title: string; note_slug: string; proposed_content: string; current_content: string; proposer_name: string | null; proposer_note: string | null; created_at: string; stale: boolean; }
interface HistItem { id: number; proposer_name: string | null; status: string; created_at: string; resolved_at: string | null; note_title: string; note_slug: string; }
interface GuidedLink { id: number; token: string; url: string; goal: string; intro: string; sub_prompt: string; spec_status: string; bind: number; single_use: number; started: number; note_title: string; note_slug: string; submitted: number; }
interface GuidedPending { id: number; name: string | null; document_md: string; goal: string; note_title: string; note_slug: string; completed_at: string | null; }

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
  const { appTz } = useAuth();
  const [links, setLinks] = useState<ShareLink[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [history, setHistory] = useState<HistItem[]>([]);
  const [guidedLinks, setGuidedLinks] = useState<GuidedLink[]>([]);
  const [guidedPending, setGuidedPending] = useState<GuidedPending[]>([]);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState<number | null>(null);
  const [openDiff, setOpenDiff] = useState<number | null>(null);
  const [openPrompt, setOpenPrompt] = useState<number | null>(null);

  async function load() {
    setLoading(true);
    try {
      const r = await get<any>("/api/shares");
      setLinks(r.links); setProposals(r.proposals); setHistory(r.history || []);
      setGuidedLinks(r.guided_links || []); setGuidedPending(r.guided_pending || []);
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
  async function resetBind(l: ShareLink) {
    if (!confirm("Reset the lock so the link can be opened on a fresh device?")) return;
    try { await post(`/api/shares/${l.id}/reset-bind`); load(); } catch { load(); }
  }
  async function accept(p: Proposal) {
    try { await post(`/api/shares/proposals/${p.id}/accept`); load(); }
    catch (e: any) { alert(e?.message || "Couldn't accept — the note may have changed since this was proposed."); load(); }
  }
  async function reject(p: Proposal) {
    setProposals((ps) => ps.filter((x) => x.id !== p.id));
    try { await post(`/api/shares/proposals/${p.id}/reject`); load(); } catch { load(); }
  }
  async function activateGuided(g: GuidedLink) {
    try { await guidedActivate(g.id); load(); } catch (e: any) { alert(e?.message || "Couldn't activate."); }
  }
  async function toggleGuidedOpt(g: GuidedLink, key: "bind" | "single_use") {
    const next = { bind: !!g.bind, single_use: !!g.single_use, [key]: !g[key] };
    setGuidedLinks((ls) => ls.map((x) => x.id === g.id ? { ...x, bind: next.bind ? 1 : 0, single_use: next.single_use ? 1 : 0 } : x));
    try { await guidedOptions(g.id, next.bind, next.single_use); } catch { load(); }
  }
  async function resetGuidedBind(g: GuidedLink) {
    if (!confirm("Forget the device this link locked to, so it can be opened fresh?")) return;
    try { await guidedResetBind(g.id); load(); } catch { load(); }
  }
  async function copyText(text: string, id: number) {
    try { await navigator.clipboard.writeText(text); setCopied(id); setTimeout(() => setCopied(null), 1500); }
    catch { prompt("Copy:", text); }
  }
  async function acceptGuided(gp: GuidedPending) {
    try { await guidedAccept(gp.id); load(); } catch (e: any) { alert(e?.message || "Couldn't save."); load(); }
  }
  async function rejectGuided(gp: GuidedPending) {
    if (!confirm("Discard this intake? Nothing will be saved.")) return;
    setGuidedPending((g) => g.filter((x) => x.id !== gp.id));
    try { await guidedReject(gp.id); load(); } catch { load(); }
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

      {guidedPending.length > 0 && (
        <>
          <div className="adv-section" style={{ marginTop: 0 }}>Intakes to review (approval #2)</div>
          {guidedPending.map((gp) => (
            <div className="card" key={"gp" + gp.id}>
              <div className="row">
                <strong>{leaf(gp.note_title)}</strong>
                <span className="badge">{gp.name ? `${gp.name} completed` : "completed"}</span>
                <span className="spacer" />
                {gp.completed_at && <span className="muted" style={{ fontSize: 12 }}>{fmtTs(gp.completed_at, appTz)}</span>}
              </div>
              <div className="md guided-doc"><ReactMarkdown>{gp.document_md || "_(empty)_"}</ReactMarkdown></div>
              <div className="row" style={{ marginTop: 8, gap: 8 }}>
                <button className="primary" onClick={() => acceptGuided(gp)}>Approve &amp; save</button>
                <button className="ghost" onClick={() => rejectGuided(gp)}>Discard</button>
              </div>
            </div>
          ))}
        </>
      )}

      {guidedLinks.length > 0 && (
        <>
          <div className="adv-section">Guided intake links</div>
          {guidedLinks.map((g) => (
            <div className="card" key={"gl" + g.id}>
              <div className="row">
                <strong>{g.goal || leaf(g.note_title)}</strong>
                <span className={"badge " + (g.spec_status === "active" ? "" : "tag-delete")}>
                  {g.spec_status === "active" ? "live" : "draft — not live"}
                </span>
                {g.submitted > 0 && <span className="badge">{g.submitted} response{g.submitted === 1 ? "" : "s"}</span>}
                <span className="spacer" />
                <Link className="ghost" to={`/note/${g.note_slug}`} style={{ fontSize: 13, padding: "4px 8px" }}>Note</Link>
              </div>
              <button className="ghost" style={{ fontSize: 12 }} onClick={() => setOpenPrompt(openPrompt === g.id ? null : g.id)}>
                {openPrompt === g.id ? "Hide" : "Review"} the AI’s instructions
              </button>
              {openPrompt === g.id && <pre className="share-diff" style={{ marginTop: 6 }}>{g.sub_prompt}</pre>}
              <div className="row" style={{ marginTop: 8, gap: 18, flexWrap: "wrap", alignItems: "center" }}>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 13, cursor: "pointer" }}>
                  <input className="share-check" type="checkbox" checked={!!g.bind} onChange={() => toggleGuidedOpt(g, "bind")} /> Lock to first device
                </label>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 7, fontSize: 13, cursor: "pointer" }}>
                  <input className="share-check" type="checkbox" checked={!!g.single_use} onChange={() => toggleGuidedOpt(g, "single_use")} /> Single use (run once)
                </label>
                {!!g.bind && g.started > 0 && (
                  <button className="ghost" style={{ fontSize: 12 }} onClick={() => resetGuidedBind(g)}>Reset lock</button>
                )}
              </div>
              {g.spec_status === "active" && (
                <div className="row" style={{ marginTop: 6, gap: 6 }}>
                  <input readOnly value={g.url} onFocus={(e) => e.currentTarget.select()} style={{ fontSize: 12 }} />
                  <button className="ghost" onClick={() => copyText(g.url, 10000 + g.id)}>{copied === 10000 + g.id ? "Copied" : "Copy"}</button>
                  <button className="ghost danger-hover" onClick={() => revoke({ id: g.id, scope: "view", note_title: g.note_title } as any)}>Revoke</button>
                </div>
              )}
              {g.spec_status !== "active" && (
                <div className="row" style={{ marginTop: 8, gap: 8 }}>
                  <button className="primary" onClick={() => activateGuided(g)}>Activate link (approval #1)</button>
                  <button className="ghost danger-hover" onClick={() => revoke({ id: g.id, scope: "view", note_title: g.note_title } as any)}>Delete</button>
                </div>
              )}
            </div>
          ))}
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
            {l.bind ? <span className="badge" title="Locked to first device">{l.bound_at ? "locked" : "lock pending"}</span> : null}
            {l.pending > 0 && <span className="badge tag-delete">{l.pending} pending</span>}
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 12 }}>{l.last_used_at ? `viewed ${fmtTs(l.last_used_at, appTz)}` : "not viewed yet"}</span>
          </div>
          <div className="row" style={{ marginTop: 6, gap: 6 }}>
            <input readOnly value={l.url} onFocus={(e) => e.currentTarget.select()} style={{ fontSize: 12 }} />
            <button className="ghost" onClick={() => copy(l)}>{copied === l.id ? "Copied" : "Copy"}</button>
            {l.bind ? <button className="ghost" onClick={() => resetBind(l)} title="Forget the locked browser so the link can be opened fresh">Reset lock</button> : null}
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
                <span className="muted" style={{ fontSize: 12 }}>{fmtTs(h.resolved_at || h.created_at, appTz)}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
