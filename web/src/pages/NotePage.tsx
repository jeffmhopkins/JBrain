import { Children, isValidElement, useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { del, get, post, put, getPlaces, personFromNote, Place } from "../api";
import { useAuth } from "../App";
import { useIsDesktop } from "../hooks";
import { fmtTs, fmtRel, expandTimeTokensMarked } from "../time";
import { linkifyAddresses, makeLinkRenderer, renderWikiLinks, stripSummarySentinels, stripLeadingTitleH1 } from "../util";
import Attachments from "../components/Attachments";
import AiAnalysisPanel from "../components/AiAnalysisPanel";
import LabImportPanel from "../components/LabImportPanel";
import TalkPanel from "../components/TalkPanel";
import { DiffView, HistoryTimeline, TimelineEntry, VersionViewer } from "../components/VersionViewer";
import { Icon } from "../components/Icon";
import { useCapability } from "../capabilities";
import { showToast, explainError } from "../toast";
import ListEditor from "../components/ListEditor";
import NoteActionsMenu from "../components/NoteActionsMenu";
import RebuildPanel from "../components/RebuildPanel";
import ShareOptions from "../components/ShareOptions";
import { Parsed, parseList, serialize } from "../lists";

interface Note {
  id: number; title: string; slug: string; content_md: string; kind: string;
  created_at: string; updated_at: string;
  lat: number | null; lon: number | null; location_label: string | null;
  backlinks: { id: number; title: string; slug: string }[];
  tags: string[];
  // Per-note governance flags (0/1; absent or 1 = on). kb_ingest: feeds KB synthesis;
  // tool_access: visible to the assistant's search/research tools.
  kb_ingest?: number;
  tool_access?: number;
  // When this note is a merged-away redirect, the canonical target (title + slug to forward to).
  redirect_to?: string | null;
  redirect_to_slug?: string | null;
}

// The ANCESTOR path of a "/"-title as a small, clickable breadcrumb (the leaf is shown
// big as the page title separately). "kb/Places/6070 …" → "kb › Places". Each crumb
// links to the Wiki focused on that folder. Returns [] for a title with no parent.
function pathCrumbs(title: string) {
  const segs = title.split("/");
  return segs.slice(0, -1).map((seg, i) => {
    const path = segs.slice(0, i + 1).join("/");
    return (
      <span key={i}>
        {i > 0 && <span className="crumb-sep">›</span>}
        <Link to={`/wiki?q=${encodeURIComponent(path)}&kind=`} className="crumb-link">{seg}</Link>
      </span>
    );
  });
}

export default function NotePage() {
  const { slug } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  // Set when we land here after following a redirect, so we can show "Redirected from".
  const redirectedFrom = (location.state as { redirectedFrom?: string } | null)?.redirectedFrom;
  const isDesktop = useIsDesktop();
  const { appTz } = useAuth();
  const llm = useCapability("llm");   // KB "Rebuild page now" runs an LLM gather→draft
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
  const [rebuilding, setRebuilding] = useState(false);   // live AI rebuild panel open
  const [showAbsTime, setShowAbsTime] = useState(false); // tap the meta time → full created/updated
  const [shareTtl, setShareTtl] = useState(0);   // link expiry in days; 0 = never
  const [shareBind, setShareBind] = useState(false);   // lock to first device
  const [shareEditable, setShareEditable] = useState(false);   // recipients can propose edits
  const [minted, setMinted] = useState<{ url: string; scope: string } | null>(null);
  const [shareCopied, setShareCopied] = useState(false);
  const [place, setPlace] = useState<Place | null>(null);   // geofence backing a loc/ note

  // A kb/Health/<Person> or kb/Finance/<…> page is a private personal record (PHI / financial PII).
  // The server force-hardens any share of it (browser-bound + finite TTL, view-only); reflect that
  // in the dialog so the owner isn't surprised: default bind on + a finite expiry, hide "Can edit".
  const lowTitle = (note?.title || "").toLowerCase();
  const isPrivate = lowTitle.startsWith("kb/health/") || lowTitle.startsWith("kb/finance/");
  useEffect(() => {
    if (sharing && isPrivate) {
      setShareBind(true);
      setShareEditable(false);
      setShareTtl((t) => (t > 0 ? t : 30));
    }
  }, [sharing, isPrivate]);

  async function remove() {
    if (!note || !confirm(`Delete “${note.title}”? It's soft-deleted (restorable from history) and the wiki will update.`)) return;
    try { await del(`/api/notes/${note.slug}`); navigate("/wiki"); }
    catch (e: any) { showToast(explainError(e, "Couldn't delete.")); }
  }

  async function mintShare(scope: "view" | "edit") {
    if (!note) return;
    try {
      const r = await post<{ url: string }>("/api/shares", { title: note.title, scope, ttl_days: shareTtl || undefined, bind: shareBind });
      setMinted({ url: r.url, scope });
      try { await navigator.clipboard.writeText(r.url); } catch { /* ignore */ }
    } catch (e: any) { alert(e?.message || "Couldn't create link."); }
  }

  async function tagAsPerson() {
    if (!note) return;
    try {
      const r = await personFromNote(note.slug);
      alert(`Tagged as person “${r.name}”. Set their colour/aliases in Advanced → People.`);
    } catch (e: any) { alert(e?.message || "Couldn't tag as person."); }
  }

  function rebuildNow() {
    // Pre-flight: the rebuild fires LLM-backed SSE (gather→draft). Don't open the panel
    // into a doomed run with no usable key — explain instead.
    if (!llm.ready) { showToast(llm.reason, "info"); return; }
    setRebuilding(true);
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

  // The three coherent visibility presets → the two underlying flags. Full (1/1),
  // Research-only (0/1), Private (0/0). Optimistic; reload on error to resync.
  async function setVisibility(p: "full" | "research" | "private") {
    if (!note) return;
    const flags = p === "full" ? { kb_ingest: true, tool_access: true }
      : p === "research" ? { kb_ingest: false, tool_access: true }
      : { kb_ingest: false, tool_access: false };
    setNote({ ...note, kb_ingest: flags.kb_ingest ? 1 : 0, tool_access: flags.tool_access ? 1 : 0 });
    try { await put(`/api/notes/${slug}/flags`, flags); }
    catch (e: any) { alert(e?.message || "Couldn't update visibility."); reload(); }
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

  // A merged-away page is a redirect: forward to the canonical article, carrying the old
  // title so the target can show a "Redirected from" note. (Replace history so Back skips it.)
  useEffect(() => {
    if (note?.redirect_to && note.redirect_to_slug && note.redirect_to_slug !== slug) {
      navigate(`/note/${note.redirect_to_slug}`, {
        replace: true, state: { redirectedFrom: note.title },
      });
    }
  }, [note, slug, navigate]);

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
      {redirectedFrom && (
        <div className="muted" style={{ marginBottom: 8, fontSize: "0.9em" }}>
          Redirected from <em>{redirectedFrom}</em>
        </div>
      )}
      <div className="note-head">
        <div className="note-head-main">
          {note.title.includes("/") && <div className="note-path">{pathCrumbs(note.title)}</div>}
          <h1 className="note-title">
            {note.title.split("/").pop()}
            {note.kind === "kb" && <span className="badge note-title-badge">KB</span>}
            {note.kind === "place" && <span className="badge note-title-badge">📍 Place</span>}
          </h1>
        </div>
        {editing === null && (
          <div className="note-head-actions">
            <NoteActionsMenu items={[
              ...(note.kind === "kb" ? [{ key: "rebuild", label: "Edit with AI", icon: "refresh",
                                          accent: true, hint: llm.ready ? "Talk to revise it" : llm.reason, onClick: rebuildNow }] : []),
              { key: "share", label: "Share", icon: "link", onClick: () => setSharing((s) => !s) },
              { key: "edit", label: note.kind === "list" ? "Edit list" : "Edit", icon: "list", onClick: startEdit },
              ...(note.kind === "kb" ? [{ key: "person", label: "Tag as person", icon: "people",
                                         hint: "Trail attribution", onClick: tagAsPerson }] : []),
              { sep: true } as const,
              { key: "delete", label: "Delete", icon: "trash", danger: true, onClick: remove },
            ]} />
          </div>
        )}
      </div>
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
      {sharing && (
        <div className="share-panel">
          {!minted ? (
            <div className="share-stack">
              <strong className="share-stack-head">Create a public link</strong>

              {isPrivate && (
                <div className="share-help" style={{ background: "var(--warn-bg, #fff6e5)", padding: "8px 10px", borderRadius: 8 }}>
                  🔒 This is a private <strong>personal record</strong>. Shared links are always
                  view-only, lock to the first device that opens them, and expire — they can't be
                  made permanent or editable.
                </div>
              )}

              {!isPrivate && (
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
                              show={{ singleUse: false, ttlMin: isPrivate ? 1 : 0 }}
                              onChange={(p) => { if (p.ttl_days !== undefined) setShareTtl(Math.max(isPrivate ? 1 : 0, p.ttl_days));
                                                 if (p.bind !== undefined) setShareBind(isPrivate ? true : p.bind); }} />
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
      {note.created_at && (() => {
        const changed = fmtTs(note.updated_at, appTz) !== fmtTs(note.created_at, appTz);
        const rel = changed ? `updated ${fmtRel(note.updated_at, appTz)}` : fmtRel(note.created_at, appTz);
        const abs = changed
          ? `${fmtTs(note.created_at, appTz)} · updated ${fmtTs(note.updated_at, appTz)}`
          : fmtTs(note.created_at, appTz);
        return (
          <div className="muted note-meta">
            <span className="note-time" onClick={() => setShowAbsTime((a) => !a)}
                  title={abs}>🕐 {showAbsTime ? abs : rel}</span>
            {note.lat != null && note.lon != null && (
              <Link to={`/map?focus=${note.slug}`} title="View on map">
                <Icon name="pin" size={13} /> {note.location_label || `${note.lat}, ${note.lon}`}
              </Link>
            )}
          </div>
        );
      })()}
      {rebuilding && (
        <RebuildPanel slug={note.slug} note={{ title: note.title, content_md: note.content_md }} mode="suggest"
          onClose={() => setRebuilding(false)}
          onAccepted={(s) => { setRebuilding(false); if (s !== note.slug) navigate(`/note/${s}`); else reload(); }} />
      )}
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
      {editing === null && note.kind !== "kb" && note.kind !== "place" && (() => {
        // Default-on: a missing flag (older payload) reads as enabled.
        const kbOn = note.kb_ingest !== 0;
        const toolOn = note.tool_access !== 0;
        const preset = kbOn ? "full" : (toolOn ? "research" : "private");
        const help = preset === "full"
          ? "Your assistant can research this, and it can be summarized into your Knowledge Base."
          : preset === "research"
          ? "Your assistant can read this when you ask — but it's never folded into Knowledge Base articles."
          : "Kept out of the Knowledge Base and out of assistant search results. Still searchable and readable by you.";
        return (
          <div className="note-visibility" style={{ marginBottom: 10 }}>
            <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span className="muted" style={{ fontSize: 12 }}>Assistant &amp; Knowledge Base</span>
              <div className="share-seg" role="group" aria-label="Visibility">
                <button className={preset === "full" ? "primary" : "ghost"} aria-pressed={preset === "full"}
                        onClick={() => setVisibility("full")}>Full</button>
                <button className={preset === "research" ? "primary" : "ghost"} aria-pressed={preset === "research"}
                        onClick={() => setVisibility("research")}>Research-only</button>
                <button className={preset === "private" ? "primary" : "ghost"} aria-pressed={preset === "private"}
                        onClick={() => setVisibility("private")}>Private</button>
              </div>
            </div>
            <div className="share-help" style={{ marginTop: 4, fontSize: 12 }}>{help}</div>
          </div>
        );
      })()}
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
          }}>{renderWikiLinks(linkifyAddresses(expandTimeTokensMarked(stripSummarySentinels(
            note.kind === "kb" ? stripLeadingTitleH1(note.content_md, note.title) : note.content_md), appTz)))}</ReactMarkdown>
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
