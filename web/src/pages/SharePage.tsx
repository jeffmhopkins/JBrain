import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { getShare, proposeShareEdit, shareAttachmentUrl } from "../api";
import { renderWikiLinks } from "../util";

interface ShareAtt { id: number; filename: string; mime: string; byte_size: number; }
interface ShareView {
  scope: "view" | "edit"; can_edit: boolean; brain_name: string;
  note: { title: string; content_md: string; kind: string; updated_at: string; attachments: ShareAtt[] };
}

// On a shared page, [[wiki-links]] (rendered as /note/ links) point at notes the
// recipient can't reach — render them as inert text, not navigable links.
function flatLink({ href, children }: any) {
  if (href?.startsWith("/note/")) return <span className="wikilink-flat">{children}</span>;
  return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
}

const leaf = (t: string) => t.replace(/^(notes|kb|lists)\//i, "");

// The title is shown as the page heading; if the body opens with the same "# Title"
// heading (lists carry one), drop it so it isn't rendered twice.
function stripTitleHeading(md: string, title: string): string {
  const want = leaf(title).trim().toLowerCase();
  return md.replace(/^\s*#\s+(.+?)[ \t]*(?:\n|$)/, (m, h) => (h.trim().toLowerCase() === want ? "" : m));
}

export default function SharePage() {
  const { token = "" } = useParams();
  const [data, setData] = useState<ShareView | null>(null);
  const [error, setError] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [pnote, setPnote] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getShare<ShareView>(token).then(setData).catch(() => setError(true));
  }, [token]);

  if (error) return (
    <div className="share-page"><div className="share-card">
      <h2>This link isn't available.</h2>
      <p className="muted">It may have been revoked, expired, or never existed.</p>
    </div></div>
  );
  if (!data) return <div className="share-page"><div className="muted">Loading…</div></div>;

  async function submit() {
    if (editing == null) return;
    setBusy(true);
    try { await proposeShareEdit(token, editing, pnote || undefined); setSent(true); }
    catch (e: any) { alert(e?.message || "Couldn't send."); }
    finally { setBusy(false); }
  }

  const n = data.note;
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
            <p className="muted">The owner will review it before it's published. Thanks!</p>
            <button className="ghost" onClick={() => { setSent(false); setEditing(null); }}>Propose another change</button>
          </div>
        ) : editing != null ? (
          <div>
            <div className="share-banner">Edits here are <strong>proposals</strong> — they're sent to the owner and aren't published until accepted.</div>
            <textarea className="note-edit-area" style={{ fontFamily: "monospace" }} value={editing}
                      onChange={(e) => setEditing(e.target.value)} />
            <input placeholder="Note to owner (optional)…" value={pnote}
                   onChange={(e) => setPnote(e.target.value)} style={{ marginTop: 8 }} />
            <div className="row" style={{ marginTop: 10, gap: 8 }}>
              <button className="primary" onClick={submit} disabled={busy}>{busy ? "Sending…" : "Propose changes"}</button>
              <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
            </div>
          </div>
        ) : (
          <>
            <div className="md">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: flatLink }}>
                {renderWikiLinks(stripTitleHeading(n.content_md, n.title))}
              </ReactMarkdown>
            </div>
            {n.attachments.length > 0 && (
              <div style={{ marginTop: 16 }}>
                <h3>Attachments</h3>
                {n.attachments.map((a) => (
                  <a key={a.id} className="list-item" href={shareAttachmentUrl(token, a.id)}
                     target="_blank" rel="noreferrer">{a.filename}</a>
                ))}
              </div>
            )}
            {data.can_edit && (
              <div className="row" style={{ marginTop: 16 }}>
                <button className="primary" onClick={() => setEditing(n.content_md)}>Suggest an edit</button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
