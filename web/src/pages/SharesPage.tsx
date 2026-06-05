import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { get, guidedAccept, guidedAcknowledge, guidedActivate, guidedDeleteSession, guidedOptions, guidedReject, guidedReopen, guidedResetBind, guidedSession, guidedSessions, guidedSetDetails, setLinkExpiry, post } from "../api";
import ShareOptions from "../components/ShareOptions";
import ResearchLinks from "../components/ResearchLinks";
import LabShareLinks from "../components/LabShareLinks";
import ConversationView from "../components/ConversationView";
import { useAuth } from "../App";
import { fmtTs, fmtTsShort } from "../time";

interface ShareLink { id: number; token: string; scope: "view" | "edit"; label: string | null; created_at: string; last_used_at: string | null; expires_at: string | null; bind: number; bound_at: string | null; pending: number; note_title: string; note_slug: string; url: string; }
interface Proposal { id: number; note_title: string; note_slug: string; proposed_content: string; current_content: string; proposer_name: string | null; proposer_note: string | null; created_at: string; stale: boolean; }
interface HistItem { id: number; proposer_name: string | null; status: string; created_at: string; resolved_at: string | null; note_title: string; note_slug: string; }
interface GuidedLink { id: number; token: string; url: string; goal: string; intro: string; sub_prompt: string; spec_status: string; bind: number; single_use: number; started: number; note_title: string | null; note_slug: string | null; submitted: number; expires_at: string | null; }
interface GuidedPending { id: number; name: string | null; document_md: string; goal: string; note_title: string | null; note_slug: string | null; completed_at: string | null; transcript: { role: string; content: string }[]; }
interface GuidedEnded { id: number; name: string | null; end_reason: string; goal: string; note_title: string | null; note_slug: string | null; link_id: number; link_status: string; completed_at: string | null; transcript: { role: string; content: string }[]; }
interface GuidedHist { id: number; name: string | null; goal: string; disposition: string; note_title: string | null; note_slug: string | null; completed_at: string | null; }

const leaf = (t: string | null) => (t || "").replace(/^(notes|kb|lists)\//i, "");
const STATUS_CLR: Record<string, string> = {
  accepted: "#4ade80", approved: "#4ade80", rejected: "var(--danger)", ended: "var(--danger)",
  superseded: "var(--text-dim)", discarded: "var(--text-dim)", distress: "#fbbf24",
};

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
  const [guidedEnded, setGuidedEnded] = useState<GuidedEnded[]>([]);
  const [guidedHistory, setGuidedHistory] = useState<GuidedHist[]>([]);
  const [researchLinks, setResearchLinks] = useState<any[]>([]);
  const [labLinks, setLabLinks] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState<number | null>(null);
  const [openDiff, setOpenDiff] = useState<number | null>(null);
  const [openPrompt, setOpenPrompt] = useState<number | null>(null);
  const [openConvo, setOpenConvo] = useState<number | null>(null);
  const [openSessions, setOpenSessions] = useState<number | null>(null);

  async function load() {
    setLoading(true);
    try {
      const r = await get<any>("/api/shares");
      setLinks(r.links); setProposals(r.proposals); setHistory(r.history || []);
      setGuidedLinks(r.guided_links || []); setGuidedPending(r.guided_pending || []);
      setGuidedEnded(r.guided_ended || []); setGuidedHistory(r.guided_history || []);
      setResearchLinks(r.research_links || []);
      setLabLinks(r.lab_links || []);
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
  async function revokeGuided(g: GuidedLink) {
    const draft = g.spec_status !== "active";
    const name = g.goal || leaf(g.note_title);
    const msg = draft
      ? `Delete this draft intake “${name}”? It was never live, so nothing is lost.`
      : `Revoke this guided intake link “${name}”? It stops working immediately and anyone mid-conversation can't continue.`;
    if (!confirm(msg)) return;
    setGuidedLinks((ls) => ls.filter((x) => x.id !== g.id));
    try { await post(`/api/shares/${g.id}/revoke`); load(); } catch { load(); }
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
  async function reopenEnded(ge: GuidedEnded) {
    if (!confirm("Re-open this link so it works again? Use this if the chat was ended by mistake.")) return;
    setGuidedEnded((g) => g.filter((x) => x.id !== ge.id));
    try { await guidedReopen(ge.id); load(); } catch { load(); }
  }
  async function ackEnded(ge: GuidedEnded) {
    setGuidedEnded((g) => g.filter((x) => x.id !== ge.id));
    try { await guidedAcknowledge(ge.id); load(); } catch { load(); }
  }
  const reasonLabel = (r: string) =>
    r === "distress" ? "may need support"
      : "ended for " + (r.split(":")[1] || "abuse").replace("jailbreak", "bypass attempts").replace("offtopic", "going off-topic");

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
                <strong>{gp.goal || "Intake"}{gp.note_title ? ` → ${leaf(gp.note_title)}` : ""}</strong>
                <span className="badge">{gp.name ? `${gp.name} completed` : "completed"}</span>
                <span className="spacer" />
                {gp.completed_at && <span className="muted" style={{ fontSize: 12 }}>{fmtTs(gp.completed_at, appTz)}</span>}
              </div>
              <div className="md guided-doc"><ReactMarkdown>{gp.document_md || "_(empty)_"}</ReactMarkdown></div>
              {gp.transcript.length > 0 && (
                <>
                  <button className="ghost" style={{ fontSize: 12, marginTop: 6 }}
                          onClick={() => setOpenConvo(openConvo === gp.id ? null : gp.id)}>
                    {openConvo === gp.id ? "Hide" : "View"} conversation ({gp.transcript.length})
                  </button>
                  {openConvo === gp.id && (
                    <div className="guided-convo">
                      <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
                        Your eyes only — not saved to your brain or searchable. Kept as a record after you decide (delete it from “View conversations”).
                      </div>
                      {gp.transcript.map((t, i) => (
                        <div key={i} className={"msg " + (t.role === "assistant" ? "assistant" : "user")}>
                          <span className="convo-who">{t.role === "assistant" ? "AI" : (gp.name || "Them")}</span>{t.content}
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
              <div className="row" style={{ marginTop: 8, gap: 8 }}>
                <button className="primary" onClick={() => acceptGuided(gp)}>Approve &amp; save</button>
                <button className="ghost" onClick={() => rejectGuided(gp)}>Discard</button>
              </div>
            </div>
          ))}
        </>
      )}

      {guidedEnded.length > 0 && (
        <>
          <div className="adv-section" style={{ marginTop: 0 }}>Auto-ended chats</div>
          {guidedEnded.map((ge) => {
            const distress = ge.end_reason === "distress";
            return (
              <div className="card" key={"ge" + ge.id}>
                <div className="row">
                  <strong>{ge.goal || leaf(ge.note_title)}</strong>
                  <span className={"badge " + (distress ? "" : "tag-delete")}>{ge.name ? `${ge.name} · ` : ""}{reasonLabel(ge.end_reason)}</span>
                  <span className="spacer" />
                  {ge.completed_at && <span className="muted" style={{ fontSize: 12 }}>{fmtTs(ge.completed_at, appTz)}</span>}
                </div>
                <div className="muted" style={{ fontSize: 13, margin: "4px 0" }}>
                  {distress
                    ? "Closed gently — they may have shared something difficult. Consider reaching out. The link was NOT locked."
                    : "The chat was ended automatically and the link was locked. Review it, then re-open the link if this was a mistake."}
                </div>
                {ge.transcript.length > 0 && (
                  <>
                    <button className="ghost" style={{ fontSize: 12 }} onClick={() => setOpenConvo(openConvo === -ge.id ? null : -ge.id)}>
                      {openConvo === -ge.id ? "Hide" : "View"} conversation ({ge.transcript.length})
                    </button>
                    {openConvo === -ge.id && (
                      <div className="guided-convo">
                        <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>Your eyes only — kept as a record after you dismiss or re-open.</div>
                        {ge.transcript.map((t, i) => (
                          <div key={i} className={"msg " + (t.role === "assistant" ? "assistant" : "user")}>
                            <span className="convo-who">{t.role === "assistant" ? "AI" : (ge.name || "Them")}</span>{t.content}
                          </div>
                        ))}
                      </div>
                    )}
                  </>
                )}
                <div className="row" style={{ marginTop: 8, gap: 8 }}>
                  {!distress && ge.link_status === "revoked" && (
                    <button className="primary" onClick={() => reopenEnded(ge)}>Re-open link</button>
                  )}
                  <button className="ghost" onClick={() => ackEnded(ge)}>Dismiss</button>
                </div>
              </div>
            );
          })}
        </>
      )}

      {guidedLinks.length > 0 && (
        <>
          <div className="adv-section">Guided intake links</div>
          {guidedLinks.map((g) => (
            <div className="card" key={"gl" + g.id}>
              <div className="row">
                <strong>{g.goal || leaf(g.note_title)}</strong>
                <span className={"badge " + (g.spec_status === "active" ? "prio" : "tag-delete")}>
                  {g.spec_status === "active" ? "live" : "draft — not live"}
                </span>
                {g.submitted > 0 && <span className="badge">{g.submitted} response{g.submitted === 1 ? "" : "s"}</span>}
                <span className="spacer" />
                {g.note_slug && <Link className="ghost" to={`/note/${g.note_slug}`} style={{ fontSize: 13, padding: "4px 8px" }}>Note</Link>}
              </div>
              <button className="ghost" style={{ fontSize: 12 }} onClick={() => setOpenPrompt(openPrompt === g.id ? null : g.id)}>
                {openPrompt === g.id ? "Hide" : "Edit"} the AI’s instructions &amp; expiry
              </button>
              {openPrompt === g.id && <GuidedEditor g={g} onSaved={load} />}
              <div className="row" style={{ marginTop: 8, gap: 18, flexWrap: "wrap", alignItems: "center" }}>
                <ShareOptions value={{ bind: !!g.bind, single_use: !!g.single_use, ttl_days: 0 }}
                              show={{ singleUse: true, expiry: false }}
                              onChange={(p) => { if ("bind" in p) toggleGuidedOpt(g, "bind");
                                                 if ("single_use" in p) toggleGuidedOpt(g, "single_use"); }} />
                {!!g.bind && g.started > 0 && (
                  <button className="ghost" style={{ fontSize: 12 }} onClick={() => resetGuidedBind(g)}>Reset lock</button>
                )}
              </div>
              {g.started > 0 && (
                <>
                  <button className="ghost" style={{ fontSize: 12, marginTop: 6 }}
                          onClick={() => setOpenSessions(openSessions === g.id ? null : g.id)}>
                    {openSessions === g.id ? "Hide" : "View"} conversations ({g.started})
                  </button>
                  {openSessions === g.id && <GuidedSessions linkId={g.id} />}
                </>
              )}
              {g.spec_status === "active" && (
                <div className="row" style={{ marginTop: 6, gap: 6 }}>
                  <input readOnly value={g.url} onFocus={(e) => e.currentTarget.select()} style={{ fontSize: 12 }} />
                  <button className="ghost" onClick={() => copyText(g.url, 10000 + g.id)}>{copied === 10000 + g.id ? "Copied" : "Copy"}</button>
                  <button className="ghost danger-hover" onClick={() => revokeGuided(g)}>Revoke</button>
                </div>
              )}
              {g.spec_status !== "active" && (
                <div className="row" style={{ marginTop: 8, gap: 8 }}>
                  <button className="primary" onClick={() => activateGuided(g)}>Activate link (approval #1)</button>
                  <button className="ghost danger-hover" onClick={() => revokeGuided(g)}>Delete</button>
                </div>
              )}
            </div>
          ))}
        </>
      )}

      <div className="adv-section">Active links</div>
      {!loading && links.length === 0 && (
        <p className="share-hint">No links yet — share a note from its page, or ask the assistant.</p>
      )}
      {links.map((l) => (
        <div className="card" key={l.id}>
          <div className="row">
            <strong>{leaf(l.note_title)}</strong>
            <span className={"badge " + (l.scope === "edit" ? "prio" : "")}>{l.scope}</span>
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
            <button className="ghost" title="Set/clear link expiry" onClick={() => {
              const v = prompt("Expire this link in how many days? (0 = never)", "0");
              if (v !== null) setLinkExpiry(l.id, Math.max(0, parseInt(v, 10) || 0)).then(load);
            }}>Expiry</button>
            <button className="ghost danger-hover" onClick={() => revoke(l)}>Revoke</button>
          </div>
        </div>
      ))}

      <ResearchLinks links={researchLinks} reload={load} />

      <LabShareLinks links={labLinks} reload={load} />

      {(history.length > 0 || guidedHistory.length > 0) && (
        <>
          <div className="adv-section">History</div>
          <div className="card share-hist">
            {guidedHistory.map((h) => (
              <HistRow key={"gh" + h.id} status={h.disposition} slug={h.note_slug}
                       title={leaf(h.goal || h.note_title)} meta={`guided · ${h.name || "someone"}`}
                       when={h.completed_at} appTz={appTz} />
            ))}
            {history.map((h) => (
              <HistRow key={h.id} status={h.status} slug={h.note_slug}
                       title={leaf(h.note_title)} meta={`edit · ${h.proposer_name || "someone"}`}
                       when={h.resolved_at || h.created_at} appTz={appTz} />
            ))}
          </div>
        </>
      )}

    </div>
  );
}

// One compact history entry: a colored status pill, the linked title, and a muted
// "<source> · <who> · <when>" line. Replaces the old single-row layout whose long
// timestamp wrapped onto three lines on a phone.
function HistRow({ status, slug, title, meta, when, appTz }: {
  status: string; slug: string | null; title: string; meta: string; when?: string | null; appTz?: string;
}) {
  return (
    <div className="share-hist-item">
      <span className="badge" style={{ color: STATUS_CLR[status] || "var(--text-dim)", textTransform: "capitalize" }}>
        {status}
      </span>
      <div className="share-hist-body">
        {slug ? <Link to={`/note/${slug}`} className="share-hist-title">{title}</Link>
              : <span className="share-hist-title">{title}</span>}
        <div className="share-hist-meta">{meta}{when ? ` · ${fmtTsShort(when, appTz)}` : ""}</div>
      </div>
    </div>
  );
}

interface GuidedSessionRow {
  id: number; name: string | null; status: string; end_reason: string | null;
  turn_count: number; created_at: string; completed_at: string | null;
  has_document: number; has_transcript: number;
}

// Every recipient conversation for a guided link — including ones still in progress or
// abandoned, which the pending/ended/history views don't surface. The transcript for a
// row is fetched lazily when expanded (kept out of the list payload).
function GuidedSessions({ linkId }: { linkId: number }) {
  const [rows, setRows] = useState<GuidedSessionRow[] | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [convo, setConvo] = useState<Record<number, any>>({});

  useEffect(() => {
    guidedSessions<{ sessions: GuidedSessionRow[] }>(linkId)
      .then((r) => setRows(r.sessions || [])).catch(() => setRows([]));
  }, [linkId]);

  async function view(sid: number) {
    if (open === sid) { setOpen(null); return; }
    setOpen(sid);
    if (!convo[sid]) {
      try { const r = await guidedSession(linkId, sid); setConvo((c) => ({ ...c, [sid]: r })); }
      catch { setConvo((c) => ({ ...c, [sid]: { transcript: [], error: true } })); }
    }
  }

  async function del(sid: number) {
    if (!confirm("Permanently delete this conversation and its drafted document?")) return;
    try { await guidedDeleteSession(sid); } catch { /* ignore */ }
    setConvo((c) => { const n = { ...c }; delete n[sid]; return n; });
    setOpen(null);
    guidedSessions<{ sessions: GuidedSessionRow[] }>(linkId).then((r) => setRows(r.sessions || [])).catch(() => {});
  }

  const label = (s: GuidedSessionRow) =>
    s.status === "submitted" ? "completed"
      : s.status === "drafting" ? "drafted — not submitted"
      : s.status === "active" ? "in progress"
      : s.end_reason?.startsWith("abuse") ? "ended (abuse)"
      : s.end_reason === "distress" ? "ended (distress)"
      : "abandoned";

  if (rows === null) return <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>Loading…</div>;
  if (rows.length === 0) return <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>No one has started this intake yet.</div>;

  return (
    <div className="guided-sessions" style={{ marginTop: 8 }}>
      {rows.map((s) => {
        const c = convo[s.id];
        const viewable = !!(s.has_transcript || s.has_document);
        return (
          <div className="guided-session" key={"gs" + s.id}>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <strong style={{ fontSize: 13 }}>{s.name || "Anonymous"}</strong>
              <span className={"badge " + (s.status === "active" || s.status === "drafting" ? "prio" : "")}>{label(s)}</span>
              <span className="muted" style={{ fontSize: 12 }}>{s.turn_count} turn{s.turn_count === 1 ? "" : "s"}</span>
              <span className="spacer" />
              {viewable
                ? <button className="ghost" style={{ fontSize: 12 }} onClick={() => view(s.id)}>{open === s.id ? "Hide" : "View"}</button>
                : <span className="muted" style={{ fontSize: 12 }}>nothing to show</span>}
            </div>
            {open === s.id && c && (
              c.error
                ? <div className="muted" style={{ fontSize: 12 }}>Couldn't load this conversation.</div>
                : <ConversationView messages={c.transcript || []} name={s.name}
                                    documentMd={c.document_md} noteSlug={c.note_slug}
                                    onDelete={() => del(s.id)} />
            )}
          </div>
        );
      })}
    </div>
  );
}

// Edit a guided link's interview brief + expiry post-creation (parity with research
// links). Prefills the expiry as remaining days so saving doesn't wipe it.
function GuidedEditor({ g, onSaved }: { g: GuidedLink; onSaved: () => void }) {
  const remaining = g.expires_at
    ? Math.max(0, Math.ceil((Date.parse(g.expires_at.replace(" ", "T") + "Z") - Date.now()) / 86400000))
    : 0;
  const [goal, setGoal] = useState(g.goal || "");
  const [intro, setIntro] = useState(g.intro || "");
  const [sub, setSub] = useState(g.sub_prompt || "");
  const [ttl, setTtl] = useState(remaining);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  async function save() {
    setBusy(true);
    try {
      await guidedSetDetails(g.id, { goal, intro, sub_prompt: sub, ttl_days: ttl });
      setSaved(true); setTimeout(() => setSaved(false), 1500); onSaved();
    } catch (e: any) { alert(e?.message || "Couldn't save."); }
    finally { setBusy(false); }
  }
  return (
    <div style={{ marginTop: 6 }}>
      <label className="share-field">Goal
        <input value={goal} disabled={busy} onChange={(e) => setGoal(e.target.value)} /></label>
      <label className="share-field">Recipient greeting (intro)
        <textarea rows={2} value={intro} disabled={busy} onChange={(e) => setIntro(e.target.value)} /></label>
      <label className="share-field">Interview instructions (the AI’s brief)
        <textarea rows={5} value={sub} disabled={busy} onChange={(e) => setSub(e.target.value)} /></label>
      <label className="row" style={{ gap: 6, fontSize: 12 }}>Expires in
        <input type="number" min={0} style={{ width: 64 }} value={ttl} disabled={busy}
               onChange={(e) => setTtl(Math.max(0, +e.target.value))} /> days (0 = never)</label>
      <button className="primary" style={{ marginTop: 6 }} disabled={busy || !sub.trim()} onClick={save}>
        {saved ? "Saved ✓" : "Save"}</button>
    </div>
  );
}
