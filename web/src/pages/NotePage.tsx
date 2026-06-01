import { Children, isValidElement, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { del, get, post, put } from "../api";
import { useAuth } from "../App";
import { useIsDesktop } from "../hooks";
import { fmtTs, expandTimeTokens } from "../time";
import { makeLinkRenderer, renderWikiLinks, stripSummarySentinels } from "../util";
import Attachments from "../components/Attachments";
import { DiffView, HistoryTimeline, TimelineEntry, VersionViewer } from "../components/VersionViewer";
import { Icon } from "../components/Icon";
import ListEditor from "../components/ListEditor";
import { Parsed, parseList, serialize } from "../lists";

interface Note {
  id: number; title: string; slug: string; content_md: string; kind: string;
  created_at: string; updated_at: string;
  lat: number | null; lon: number | null; location_label: string | null;
  backlinks: { id: number; title: string; slug: string }[];
  tags: string[];
}

// Long path titles (e.g. notes/daily/2026/06/01/3) wrap cleanly at the slashes
// instead of breaking mid-number; <wbr> adds a break opportunity, copies as "/".
function breakableTitle(title: string) {
  return title.split("/").map((seg, i) => (
    <span key={i}>{i > 0 && <>/<wbr /></>}{seg}</span>
  ));
}

export default function NotePage() {
  const { slug } = useParams();
  const navigate = useNavigate();
  const isDesktop = useIsDesktop();
  const { appTz } = useAuth();
  const [note, setNote] = useState<Note | null>(null);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [error, setError] = useState("");
  const [viewing, setViewing] = useState<TimelineEntry | null>(null);
  const [diffing, setDiffing] = useState<{ from: TimelineEntry; to: TimelineEntry } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [listModel, setListModel] = useState<Parsed | null>(null);   // card editor for list notes
  const [saving, setSaving] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [shareTtl, setShareTtl] = useState(0);   // link expiry in days; 0 = never
  const [shareBind, setShareBind] = useState(false);   // lock to first device
  const [minted, setMinted] = useState<{ url: string; scope: string } | null>(null);

  async function remove() {
    if (!note || !confirm(`Delete “${note.title}”? It's soft-deleted (restorable from history) and the wiki will update.`)) return;
    try { await del(`/api/notes/${note.slug}`); navigate("/wiki"); }
    catch (e: any) { alert(e?.message || "Couldn't delete."); }
  }

  async function mintShare(scope: "view" | "edit") {
    if (!note) return;
    try {
      const r = await post<{ url: string }>("/api/shares", { title: note.title, scope, ttl_days: shareTtl || undefined, bind: shareBind });
      setMinted({ url: r.url, scope });
      try { await navigator.clipboard.writeText(r.url); } catch { /* ignore */ }
    } catch (e: any) { alert(e?.message || "Couldn't create link."); }
  }

  function startEdit() {
    if (!note) return;
    setEditTitle(note.title);
    setListModel(note.kind === "list" ? parseList(note.content_md) : null);   // card editor for lists
    setEditing(note.content_md);
  }

  async function saveEdit() {
    if (!note) return;
    const title = editTitle.trim();
    if (!title) { alert("Title can't be empty."); return; }
    const content_md = listModel ? serialize(listModel) : editing;
    setSaving(true);
    try {
      // PUT renames in place (id-targeted); use it to move notes under notes//kb/.
      const r = await put<{ slug: string }>(`/api/notes/${note.slug}`, { title, content_md });
      setEditing(null);
      if (r.slug !== note.slug) navigate(`/note/${r.slug}`);  // title changed -> slug changed
      else reload();
    } catch (e: any) {
      alert(e?.message || "Couldn't save.");
    } finally {
      setSaving(false);
    }
  }

  function reload() {
    get<Note>(`/api/notes/${slug}`).then(setNote).catch((e) => setError(e.message));
    get<TimelineEntry[]>(`/api/notes/${slug}/versions`).then(setTimeline).catch(() => {});
  }

  // Toggle a `- [ ]`/`- [x]` checkbox on the given (0-based) source line and save.
  async function toggleCheckbox(lineIdx: number) {
    if (!note) return;
    const lines = note.content_md.split("\n");
    const ln = lines[lineIdx];
    if (!ln || !/\[[ xX]\]/.test(ln)) return;
    lines[lineIdx] = ln.replace(/\[[ xX]\]/, /\[ \]/.test(ln) ? "[x]" : "[ ]");
    const content_md = lines.join("\n");
    setNote({ ...note, content_md });   // optimistic
    try { await put(`/api/notes/${note.slug}`, { title: note.title, content_md }); }
    catch { reload(); }
  }

  useEffect(() => {
    setNote(null); setError("");
    reload();
  }, [slug]);

  if (error) return <div className="content"><p className="muted">{error}</p><Link to="/wiki">← Back to wiki</Link></div>;
  if (!note) return <div className="content muted">Loading…</div>;
  const sourceLines = note.content_md.split("\n");   // for mapping rendered checkboxes back to source lines

  const rail = (
    <>
      <h3 style={{ marginTop: 0 }}>Attachments</h3>
      <Attachments slug={note.slug} onNoteChanged={reload} />

      <h3 style={{ marginTop: 20 }}>Backlinks</h3>
      {note.backlinks.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No notes link here yet.</p>}
      <div className="backlink-row">
        {note.backlinks.map((b) => (
          <Link key={b.id} to={`/note/${b.slug}`} className="backlink-chip" title={b.title}>
            <Icon name="link" size={12} /> {b.title.replace(/^(notes|kb)\//i, "")}
          </Link>
        ))}
      </div>

      <h3 style={{ marginTop: 20 }}>History</h3>
      <HistoryTimeline timeline={timeline} onView={setViewing} onDiff={(from, to) => setDiffing({ from, to })} />
    </>
  );

  const article = (
    <div className="content">
      <h1 className="note-title">
        {breakableTitle(note.title)}
        {note.kind === "kb" && <span className="badge" style={{ marginLeft: 8, verticalAlign: "middle" }}>KB</span>}
      </h1>
      {editing === null && (
        <div className="row" style={{ marginTop: 10, gap: 8 }}>
          <button className="ghost" onClick={() => setSharing((s) => !s)}>Share</button>
          <button className="ghost" onClick={startEdit}>{note.kind === "list" ? "Edit list" : "Edit"}</button>
          <button className="ghost danger-hover" onClick={remove}>Delete</button>
        </div>
      )}
      {sharing && (
        <div className="share-panel">
          {!minted ? (
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <span className="muted" style={{ fontSize: 13 }}>Create a public link:</span>
              <button className="ghost" onClick={() => mintShare("view")}>View-only</button>
              <button className="ghost" onClick={() => mintShare("edit")}>Editable (proposals)</button>
              <span className="spacer" />
              <select value={shareTtl} onChange={(e) => setShareTtl(Number(e.target.value))}
                      style={{ width: "auto", fontSize: 13, padding: "6px 8px" }} title="Link expiry">
                <option value={0}>No expiry</option>
                <option value={1}>Expires in 1 day</option>
                <option value={7}>Expires in 7 days</option>
                <option value={30}>Expires in 30 days</option>
              </select>
              <label className="row" style={{ gap: 4, fontSize: 13 }} title="Ties the link to the first BROWSER that opens it — others get a 'locked' page (resettable). Friction against re-sharing, not strong security.">
                <input type="checkbox" style={{ width: 16, height: 16 }} checked={shareBind}
                       onChange={(e) => setShareBind(e.target.checked)} /> Lock to first browser
              </label>
            </div>
          ) : (
            <div className="row" style={{ gap: 6 }}>
              <span className="badge">{minted.scope}</span>
              <input readOnly value={minted.url} onFocus={(e) => e.currentTarget.select()} style={{ fontSize: 12 }} />
              <button className="ghost" onClick={() => { navigator.clipboard?.writeText(minted.url); }}>Copy</button>
              <button className="ghost" onClick={() => setMinted(null)}>New</button>
            </div>
          )}
        </div>
      )}
      <div className="muted" style={{ fontSize: 12, margin: "8px 0", display: "flex", gap: 12, flexWrap: "wrap" }}>
        <span>🕐 {fmtTs(note.created_at, appTz)}{fmtTs(note.updated_at, appTz) !== fmtTs(note.created_at, appTz) ? ` · updated ${fmtTs(note.updated_at, appTz)}` : ""}</span>
        {note.lat != null && note.lon != null && (
          <a href={`https://www.openstreetmap.org/?mlat=${note.lat}&mlon=${note.lon}#map=15/${note.lat}/${note.lon}`}
             target="_blank" rel="noreferrer">
            <Icon name="pin" size={13} /> {note.location_label || `${note.lat}, ${note.lon}`}
          </a>
        )}
      </div>
      {note.tags.length > 0 && (
        <div className="row" style={{ gap: 6, marginBottom: 8 }}>
          {note.tags.map((t) => <span key={t} className="badge">#{t}</span>)}
        </div>
      )}
      {editing !== null ? (
        <div>
          <input value={editTitle} onChange={(e) => setEditTitle(e.target.value)}
                 placeholder="Title — e.g. notes/Jeff or kb/Jeff" style={{ marginBottom: 8 }} />
          {listModel ? (
            <ListEditor value={listModel} onChange={setListModel} />
          ) : (
            <textarea className="note-edit-area" style={{ fontFamily: "monospace" }} value={editing}
                      onChange={(e) => setEditing(e.target.value)} />
          )}
          <div className="row" style={{ marginTop: 10 }}>
            <button className="primary" onClick={saveEdit} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
            <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
          </div>
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            Renaming updates links from other notes automatically. Use “notes/…” for captures, “kb/…” for articles.
          </p>
        </div>
      ) : (
        <div className="md">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
            a: makeLinkRenderer(navigate),
            li: ({ node, children, className, ...props }: any) => {
              const cls = Array.isArray(className) ? className.join(" ") : (className || "");
              const line = node?.position?.start?.line;
              if (cls.includes("task-list-item") && line) {
                const src = sourceLines[line - 1] || "";
                const checked = /\[[xX]\]/.test(src);
                // Drop react-markdown's own disabled checkbox; render an interactive one.
                const kids = Children.toArray(children).filter((c) => !(isValidElement(c) && (c as any).type === "input"));
                return (
                  <li className="task-li">
                    <input type="checkbox" checked={checked} onChange={() => toggleCheckbox(line - 1)} />
                    <span className={checked ? "task-done" : ""}>{kids}</span>
                  </li>
                );
              }
              return <li className={cls || undefined} {...props}>{children}</li>;
            },
          }}>{renderWikiLinks(expandTimeTokens(stripSummarySentinels(note.content_md), appTz))}</ReactMarkdown>
        </div>
      )}
      {!isDesktop && <div style={{ marginTop: 24 }}>{rail}</div>}
    </div>
  );

  return (
    <>
      {isDesktop ? (
        <div className="with-rail">
          {article}
          <aside className="rail">{rail}</aside>
        </div>
      ) : article}

      {viewing && (
        <VersionViewer slug={note.slug} version={viewing}
          onClose={() => setViewing(null)}
          onRestored={() => { setViewing(null); reload(); }} />
      )}
      {diffing && (
        <DiffView slug={note.slug} from={diffing.from} to={diffing.to} onClose={() => setDiffing(null)} />
      )}
    </>
  );
}
