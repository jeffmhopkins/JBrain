import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { claimShare, getShare, proposeShareEdit, shareAttachmentUrl } from "../api";
import { renderWikiLinks, stripSummarySentinels } from "../util";
import { fmtTs, expandTimeTokens } from "../time";
import GuidedChat from "../components/GuidedChat";
import ResearchChat from "../components/ResearchChat";
import LabShareView from "../components/LabShareView";
import ListEditor from "../components/ListEditor";
import { Parsed, parseList, serialize } from "../lists";

interface ShareAtt { id: number; filename: string; mime: string; byte_size: number; }
interface ShareView {
  requires_claim?: boolean; allow_chat?: boolean; has_labs?: boolean;
  kind?: string; intro?: string; consent?: string; goal?: string;
  scope: "view" | "edit"; can_edit: boolean; brain_name: string; app_tz?: string; bound_name?: string | null;
  note?: { title: string; content_md: string; kind: string; updated_at: string; attachments: ShareAtt[] };
}

function flatLink({ href, children }: any) {
  if (href?.startsWith("/note/")) return <span className="wikilink-flat">{children}</span>;
  return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
}
const leaf = (t: string) => t.replace(/^(notes|kb|lists)\//i, "");
function stripTitleHeading(md: string, title: string): string {
  const want = leaf(title).trim().toLowerCase();
  return md.replace(/^\s*#\s+(.+?)[ \t]*(?:\n|$)/, (m, h) => (h.trim().toLowerCase() === want ? "" : m));
}

export default function SharePage() {
  const { token = "" } = useParams();
  const [data, setData] = useState<ShareView | null>(null);
  const [error, setError] = useState<number | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [model, setModel] = useState<Parsed | null>(null);
  const [pname, setPname] = useState(() => localStorage.getItem("jbrain_share_name") || "");
  const [pnote, setPnote] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getShare<ShareView>(token).then(setData).catch((e) => setError(e?.status || 404));
  }, [token]);

  if (error) return (
    <div className="share-page"><div className="share-card">
      <h2>{error === 429 ? "Too many requests." : error === 403 ? "This link is locked." : "This link isn't available."}</h2>
      <p className="muted">{error === 429 ? "Please wait a moment and reload."
        : error === 403 ? "It was opened in another browser first. Ask the owner to reset the lock."
        : "It may have been revoked, expired, or never existed."}</p>
    </div></div>
  );
  if (!data) return <div className="share-page"><div className="muted">Loading…</div></div>;

  // --- Guided AI intake: a separate, self-contained recipient experience ---
  if (data.kind === "guided") {
    return <GuidedChat token={token} brainName={data.brain_name}
                       intro={data.intro} consent={data.consent} goal={data.goal} />;
  }

  // --- Research Q&A: scope-bounded question answering ----------------------
  if (data.kind === "research") {
    return <ResearchChat token={token} brainName={data.brain_name} intro={data.intro} hasLabs={data.has_labs} />;
  }

  // --- Lab share: scoped trend charts (+ optional scoped chat) -------------
  if (data.kind === "labs") {
    return <LabShareView token={token} brainName={data.brain_name} intro={data.intro}
                         consent={data.consent} allowChat={data.allow_chat} />;
  }

  // --- Consent landing for a not-yet-accepted bind link --------------------
  async function accept() {
    if (data!.can_edit && !pname.trim()) { alert("Please enter your name."); return; }
    setBusy(true);
    try {
      const r = await claimShare<ShareView>(token, pname.trim() || undefined);
      if (pname.trim()) localStorage.setItem("jbrain_share_name", pname.trim());
      setData(r);
    } catch (e: any) {
      // Another tab in THIS browser may have claimed it a beat earlier — the cookie
      // could be set now, so re-read before showing the "locked" error.
      try {
        const r2 = await getShare<ShareView>(token);
        if (!r2.requires_claim) { setData(r2); return; }
      } catch { /* fall through */ }
      setError(e?.status || 403);
    } finally { setBusy(false); }
  }

  if (data.requires_claim) {
    return (
      <div className="share-page"><div className="share-card">
        <div className="share-head">
          <span className="brand">{data.brain_name}<span className="dot">.</span></span>
          <span className="badge">{data.can_edit ? "Shared · editable" : "Shared · read-only"}</span>
        </div>
        <h2 style={{ marginTop: 12 }}>{data.can_edit ? "Accept to start editing" : "Accept to view"}</h2>
        <p className="muted">{data.can_edit
          ? "Once you accept, you'll only be able to propose edits from this browser. Your name is shown to the owner on each suggestion."
          : "Once you accept, only this browser will be able to open this link. (If it lands on the wrong browser — or you clear your browser data — ask the sender to reset it.)"}</p>
        {data.can_edit && (
          <input placeholder="Your name *" value={pname} onChange={(e) => setPname(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") accept(); }} />
        )}
        <div className="row" style={{ marginTop: 12 }}>
          <button className="primary" disabled={busy} onClick={accept}>
            {busy ? "…" : data.can_edit ? "Accept & continue" : "Accept & view"}
          </button>
        </div>
      </div></div>
    );
  }

  const n = data.note!;
  const isList = n.kind === "list";
  const haveName = !!data.bound_name;   // captured at the consent landing; don't re-ask

  function startEdit() {
    setEditing(true);
    if (isList) setModel(parseList(n.content_md));
    else setDraft(n.content_md);
  }
  async function submit() {
    const name = (data!.bound_name || pname).trim();
    if (!name) { alert("Please enter your name."); return; }
    if (!haveName) localStorage.setItem("jbrain_share_name", name);
    const content = isList && model ? serialize(model) : draft;
    setBusy(true);
    try { await proposeShareEdit(token, content, name, pnote || undefined); setSent(true); }
    catch (e: any) { alert(e?.message || "Couldn't send."); }
    finally { setBusy(false); }
  }

  return (
    <div className="share-page">
      <div className="share-card">
        <div className="share-head">
          <span className="brand">{data.brain_name}<span className="dot">.</span></span>
          <span className="badge">{data.can_edit ? "Shared · editable" : "Shared · read-only"}</span>
        </div>
        <h1 style={{ marginTop: 8 }}>{leaf(n.title)}</h1>

        {sent ? (
          <div className="share-sent">
            <h3>Your edit was sent for approval.</h3>
            <p className="muted">{(data.bound_name || pname).trim()}, the owner will review it before it's published. Thanks!</p>
            <button className="ghost" onClick={() => { setSent(false); setEditing(false); }}>Propose another change</button>
          </div>
        ) : editing ? (
          <div>
            <div className="share-banner">Edits here are <strong>proposals</strong> — they're sent to the owner and aren't published until accepted.</div>
            {isList && model ? (
              <ListEditor value={model} onChange={setModel} showQueueToggle={false} />
            ) : (
              <textarea className="note-edit-area" style={{ fontFamily: "monospace" }} value={draft}
                        onChange={(e) => setDraft(e.target.value)} />
            )}
            {!haveName && <input placeholder="Your name *" value={pname} onChange={(e) => setPname(e.target.value)} style={{ marginTop: 8 }} />}
            <input placeholder="Note to owner (optional)…" value={pnote} onChange={(e) => setPnote(e.target.value)} style={{ marginTop: 6 }} />
            <div className="row" style={{ marginTop: 10, gap: 8 }}>
              <button className="primary" onClick={submit} disabled={busy}>{busy ? "Sending…" : "Propose changes"}</button>
              <button className="ghost" onClick={() => setEditing(false)}>Cancel</button>
            </div>
          </div>
        ) : (
          <>
            <div className="md">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: flatLink }}>
                {renderWikiLinks(expandTimeTokens(stripSummarySentinels(stripTitleHeading(n.content_md, n.title)), data.app_tz))}
              </ReactMarkdown>
            </div>
            {n.attachments.length > 0 && (
              <div style={{ marginTop: 16 }}>
                <h3>Attachments</h3>
                {n.attachments.map((a) => (
                  ["image/png", "image/jpeg", "image/gif", "image/webp"].includes(a.mime)
                    ? <img key={a.id} src={shareAttachmentUrl(token, a.id)} alt={a.filename}
                           style={{ maxWidth: "100%", borderRadius: 8, margin: "6px 0" }} />
                    : <a key={a.id} className="list-item" href={shareAttachmentUrl(token, a.id)}
                         target="_blank" rel="noreferrer">{a.filename}</a>
                ))}
              </div>
            )}
            {data.can_edit && (
              <div className="row" style={{ marginTop: 16 }}>
                <button className="primary" onClick={startEdit}>{isList ? "Edit list" : "Suggest an edit"}</button>
              </div>
            )}
          </>
        )}
        <p className="muted" style={{ fontSize: 12, marginTop: 20 }}>
          Shared from {data.brain_name} · updated {fmtTs(n.updated_at, data.app_tz)}
        </p>
      </div>
    </div>
  );
}
