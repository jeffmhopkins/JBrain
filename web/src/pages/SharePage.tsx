import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { claimShare, getShare, proposeShareEdit, shareAttachmentUrl } from "../api";
import { renderWikiLinks } from "../util";
import { Icon } from "../components/Icon";
import { Parsed, parseList, serialize } from "../lists";

interface ShareAtt { id: number; filename: string; mime: string; byte_size: number; }
interface ShareView {
  requires_claim?: boolean;
  scope: "view" | "edit"; can_edit: boolean; brain_name: string; bound_name?: string | null;
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
  const [newItem, setNewItem] = useState("");
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

  // --- Consent landing for a not-yet-accepted bind link --------------------
  async function accept() {
    if (data!.can_edit && !pname.trim()) { alert("Please enter your name."); return; }
    setBusy(true);
    try {
      const r = await claimShare<ShareView>(token, pname.trim() || undefined);
      if (pname.trim()) localStorage.setItem("jbrain_share_name", pname.trim());
      setData(r);
    } catch (e: any) { setError(e?.status || 403); }
    finally { setBusy(false); }
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
          : "Once you accept, only this browser will be able to open this link. (If it lands on the wrong browser, ask the sender to reset it.)"}</p>
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
  function up(fn: (p: Parsed) => void) {
    if (!model) return;
    const c: Parsed = { header: model.header, queue: model.queue, items: model.items.map((i) => ({ ...i })) };
    fn(c); setModel(c);
  }
  function addItem() {
    const t = newItem.trim();
    if (!t) return;
    setNewItem("");
    up((p) => { p.items.push({ checked: false, text: t, priority: null }); });
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
              <>
                <div className="checklist">
                  {model.items.map((it, i) => (
                    <div key={i} className="check-row">
                      <label className={"check-item" + (it.checked ? " done" : "")}>
                        <input type="checkbox" checked={it.checked}
                               onChange={(e) => up((p) => { p.items[i].checked = e.target.checked; })} />
                        {model.queue && <span className="badge prio">{i + 1}</span>}
                        <span className="check-text">{it.text}</span>
                      </label>
                      <span className="reorder">
                        <button className="reorder-btn" title="Up" disabled={i === 0}
                                onClick={() => up((p) => { [p.items[i - 1], p.items[i]] = [p.items[i], p.items[i - 1]]; })}><Icon name="chevron" size={14} /></button>
                        <button className="reorder-btn down" title="Down" disabled={i === model.items.length - 1}
                                onClick={() => up((p) => { [p.items[i + 1], p.items[i]] = [p.items[i], p.items[i + 1]]; })}><Icon name="chevron" size={14} /></button>
                        <button className="ghost" style={{ padding: "2px 6px" }} title="Remove"
                                onClick={() => up((p) => { p.items.splice(i, 1); })}>✕</button>
                      </span>
                    </div>
                  ))}
                  {model.items.length === 0 && <span className="muted" style={{ fontSize: 13 }}>Empty list.</span>}
                </div>
                <div className="row" style={{ marginTop: 8, gap: 6 }}>
                  <input placeholder="Add an item…" value={newItem}
                         onChange={(e) => setNewItem(e.target.value)}
                         onKeyDown={(e) => { if (e.key === "Enter") addItem(); }} />
                  <button className="ghost" onClick={addItem}>Add</button>
                </div>
              </>
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
                {renderWikiLinks(stripTitleHeading(n.content_md, n.title))}
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
          Shared from {data.brain_name} · updated {n.updated_at.replace(/\.\d+$/, "")}
        </p>
      </div>
    </div>
  );
}
