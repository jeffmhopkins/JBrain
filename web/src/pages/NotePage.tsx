import { Children, isValidElement, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { del, get, post, put, getPlaces, personFromNote, Place } from "../api";
import { useAuth } from "../App";
import { useIsDesktop } from "../hooks";
import { fmtTs, expandTimeTokensMarked } from "../time";
import { makeLinkRenderer, renderWikiLinks, stripSummarySentinels } from "../util";
import Attachments from "../components/Attachments";
import AiAnalysisPanel from "../components/AiAnalysisPanel";
import LabImportPanel from "../components/LabImportPanel";
import TalkPanel from "../components/TalkPanel";
import { DiffView, HistoryTimeline, TimelineEntry, VersionViewer } from "../components/VersionViewer";
import { Icon } from "../components/Icon";
import ListEditor from "../components/ListEditor";
import ShareOptions from "../components/ShareOptions";
import { Parsed, parseList, serialize } from "../lists";

interface Note {
  id: number; title: string; slug: string; content_md: string; kind: string;
  created_at: string; updated_at: string;
  lat: number | null; lon: number | null; location_label: string | null;
  backlinks: { id: number; title: string; slug: string }[];
  tags: string[];
}

// Render a "/"-path title as a clickable directory breadcrumb: each ancestor
// segment links to the Wiki focused on that folder; the note's own name (the last
// segment) stays plain. Long titles still wrap cleanly at the slashes (<wbr>).
function titleCrumbs(title: string) {
  const segs = title.split("/");
  return segs.map((seg, i) => {
    const isLast = i === segs.length - 1;
    const path = segs.slice(0, i + 1).join("/");
    return (
      <span key={i}>
        {i > 0 && <span className="crumb-sep">/<wbr /></span>}
        {isLast ? seg : (
          <Link to={`/wiki?q=${encodeURIComponent(path)}&kind=`} className="crumb-link">{seg}</Link>
        )}
      </span>
    );
  });
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
  const [shareEditable, setShareEditable] = useState(false);   // recipients can propose edits
  const [minted, setMinted] = useState<{ url: string; scope: string } | null>(null);
  const [shareCopied, setShareCopied] = useState(false);
  const [place, setPlace] = useState<Place | null>(null);   // geofence backing a loc/ note

  // A kb/Health/<Person> page is a personal medical record (PHI). The server force-hardens any
  // share of it (browser-bound + finite TTL, view-only); reflect that in the dialog so the owner
  // isn't surprised: default bind on + a finite expiry, hide the "Can edit" option.
  const isHealth = (note?.title || "").toLowerCase().startsWith("kb/health/");
  useEffect(() => {
    if (sharing && isHealth) {
      setShareBind(true);
      setShareEditable(false);
      setShareTtl((t) => (t > 0 ? t : 30));
    }
  }, [sharing, isHealth]);

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

  async function saveTags(next: string[]) {
    try { await put(`/api/notes/${slug}/tags`, { tags: next }); reload(); }
    catch (e: any) { alert(e?.message || "Couldn't update tags."); }
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

  // For a loc/ place note, pull the geofence it backs (matched by note_slug, else by
  // name) so the page can show the API specifics above the note's content.
  useEffect(() => {
    if (!note || !note.title.toLowerCase().startsWith("loc/")) { setPlace(null); return; }
    const name = note.title.slice(4);
    getPlaces()
      .then((ps) => setPlace(ps.find((p) => p.note_slug === note.slug)
        || ps.find((p) => p.name.toLowerCase() === name.toLowerCase()) || null))
      .catch(() => setPlace(null));
  }, [note?.slug, note?.title]);

  if (error) return <div className="content"><p className="muted">{error}</p><Link to="/wiki">← Back to wiki</Link></div>;
  if (!note) return <div className="content muted">Loading…</div>;
  const sourceLines = note.content_md.split("\n");   // for mapping rendered checkboxes back to source lines

  // Lab extraction is a medical-domain affordance: offer it only on notes filed under
  // notes/medical/<dest>/ (the medical-capture tree), never on ordinary entries — so a
  // random PDF/photo on a non-medical note never shows an "Extract lab values" option.
  const isMedical = note.title.toLowerCase().startsWith("notes/medical/");

  const rail = (
    <>
      <h3 style={{ marginTop: 0 }}>Attachments</h3>
      <Attachments slug={note.slug} onNoteChanged={reload} />

      {note.kind !== "kb" && <AiAnalysisPanel slug={note.slug} />}
      {note.kind !== "kb" && isMedical && <LabImportPanel slug={note.slug} />}
      {note.kind === "kb" && !note.title.includes("/_") && <TalkPanel slug={note.slug} />}

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
      <HistoryTimeline timeline={timeline} onView={setViewing} onDiff={(from, to) => setDiffing({ from, to })}
        limit={3} seeAllHref={`/note/${note.slug}/history`} />
    </>
  );

  const article = (
    <div className="content">
      <h1 className="note-title">
        {titleCrumbs(note.title)}
        {note.kind === "kb" && <span className="badge" style={{ marginLeft: 8, verticalAlign: "middle" }}>KB</span>}
        {note.kind === "place" && <span className="badge" style={{ marginLeft: 8, verticalAlign: "middle" }}>📍 Place</span>}
      </h1>
      {note.kind === "place" && (
        <div className="geofence-card">
          {place ? (
            <>
              <Icon name="pin" size={14} />
              <span>Geofence · {place.radius_m} m radius</span>
              <span className="spacer" />
              <Link to={`/map?place=${place.id}`}>View on map</Link>
            </>
          ) : (
            <>
              <Icon name="pin" size={14} />
              <span className="muted">No geofence yet.</span>
              <Link to="/map">Add one on the Map</Link>
            </>
          )}
        </div>
      )}
      {editing === null && (
        <div className="row" style={{ marginTop: 10, gap: 8, justifyContent: "flex-end" }}>
          {note.kind === "kb" && (
            <button className="ghost" title="Make this KB page a Person (for trail attribution)"
                    onClick={async () => {
                      try {
                        const r = await personFromNote(note.slug);
                        alert(`Tagged as person “${r.name}”. Set their colour/aliases in Advanced → People.`);
                      } catch (e: any) { alert(e?.message || "Couldn't tag as person."); }
                    }}>Tag as person</button>
          )}
          <button className="ghost" onClick={() => setSharing((s) => !s)}>Share</button>
          <button className="ghost" onClick={startEdit}>{note.kind === "list" ? "Edit list" : "Edit"}</button>
          <button className="ghost danger-hover" onClick={remove}>Delete</button>
        </div>
      )}
      {sharing && (
        <div className="share-panel">
          {!minted ? (
            <div className="share-stack">
              <strong className="share-stack-head">Create a public link</strong>

              {isHealth && (
                <div className="share-help" style={{ background: "var(--warn-bg, #fff6e5)", padding: "8px 10px", borderRadius: 8 }}>
                  🔒 This is a private <strong>health record</strong>. Shared links are always
                  view-only, lock to the first device that opens them, and expire — they can't be
                  made permanent or editable.
                </div>
              )}

              {!isHealth && (
                <div className="share-row">
                  <div className="share-row-label">Who can use it</div>
                  <div className="share-control">
                    <div className="share-seg" role="group" aria-label="Link access">
                      <button className={!shareEditable ? "primary" : "ghost"} aria-pressed={!shareEditable}
                              onClick={() => setShareEditable(false)}>View only</button>
                      <button className={shareEditable ? "primary" : "ghost"} aria-pressed={shareEditable}
                              onClick={() => setShareEditable(true)}>Can edit</button>
                    </div>
                    <div className="share-help">
                      {shareEditable
                        ? "Recipients can submit edits — they come back as proposals for you to approve, never applied live."
                        : "Recipients can read the note but not change it."}
                    </div>
                  </div>
                </div>
              )}

              <div className="share-row" style={{ display: "block" }}>
                <ShareOptions value={{ bind: shareBind, single_use: false, ttl_days: shareTtl }}
                              show={{ singleUse: false, ttlMin: isHealth ? 1 : 0 }}
                              onChange={(p) => { if (p.ttl_days !== undefined) setShareTtl(Math.max(isHealth ? 1 : 0, p.ttl_days));
                                                 if (p.bind !== undefined) setShareBind(isHealth ? true : p.bind); }} />
                <div className="share-help" style={{ marginTop: 4 }}>
                  Lock ties the link to the first browser that opens it; others get a resettable lock page. Friction against re-sharing, not strong security.
                </div>
              </div>

              <div className="share-actions">
                <button className="ghost" onClick={() => setSharing(false)}>Cancel</button>
                <button className="primary" onClick={() => mintShare(shareEditable ? "edit" : "view")}>Create link</button>
              </div>
            </div>
          ) : (
            <div className="share-stack">
              <div className="share-stack-head share-minted-head">
                <strong>Link created</strong>
                <span className="badge">{minted.scope === "edit" ? "Can edit" : "View only"}</span>
              </div>
              <input className="share-url" readOnly value={minted.url} onFocus={(e) => e.currentTarget.select()} />
              <div className="share-actions">
                <button className="ghost" onClick={() => setMinted(null)}>New link</button>
                <button className="primary" onClick={() => { navigator.clipboard?.writeText(minted.url);
                  setShareCopied(true); setTimeout(() => setShareCopied(false), 1200); }}>
                  {shareCopied ? "Copied ✓" : "Copy"}</button>
              </div>
            </div>
          )}
        </div>
      )}
      <div className="muted" style={{ fontSize: 12, margin: "8px 0", display: "flex", gap: 12, flexWrap: "wrap" }}>
        <span>🕐 {fmtTs(note.created_at, appTz)}{fmtTs(note.updated_at, appTz) !== fmtTs(note.created_at, appTz) ? ` · updated ${fmtTs(note.updated_at, appTz)}` : ""}</span>
        {note.lat != null && note.lon != null && (
          <Link to={`/map?focus=${note.slug}`} title="View on map">
            <Icon name="pin" size={13} /> {note.location_label || `${note.lat}, ${note.lon}`}
          </Link>
        )}
      </div>
      <div className="row tag-editor" style={{ gap: 6, marginBottom: 8, flexWrap: "wrap", alignItems: "center" }}>
        {note.tags.map((t) => (
          <span key={t} className="badge tag-edit">#{t}
            <button className="tag-x" title="Remove tag" onClick={() => saveTags(note.tags.filter((x) => x !== t))}>×</button>
          </span>
        ))}
        <input className="tag-add" placeholder="+ tag"
               onKeyDown={(e) => {
                 if (e.key === "Enter") {
                   const v = e.currentTarget.value.trim().toLowerCase().replace(/^#/, "");
                   if (v && !note.tags.includes(v)) saveTags([...note.tags, v]);
                   e.currentTarget.value = "";
                 }
               }} />
      </div>
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
          }}>{renderWikiLinks(expandTimeTokensMarked(stripSummarySentinels(note.content_md), appTz))}</ReactMarkdown>
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
