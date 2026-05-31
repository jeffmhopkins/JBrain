import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { get, post } from "../api";
import { useIsDesktop } from "../hooks";
import { makeLinkRenderer, renderWikiLinks } from "../util";
import Attachments from "../components/Attachments";
import { DiffView, HistoryTimeline, TimelineEntry, VersionViewer } from "../components/VersionViewer";

interface Note {
  id: number; title: string; slug: string; content_md: string; kind: string;
  created_at: string; updated_at: string;
  lat: number | null; lon: number | null; location_label: string | null;
  backlinks: { id: number; title: string; slug: string }[];
  tags: string[];
}

export default function NotePage() {
  const { slug } = useParams();
  const navigate = useNavigate();
  const isDesktop = useIsDesktop();
  const [note, setNote] = useState<Note | null>(null);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [error, setError] = useState("");
  const [viewing, setViewing] = useState<TimelineEntry | null>(null);
  const [diffing, setDiffing] = useState<{ from: TimelineEntry; to: TimelineEntry } | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function saveEdit() {
    if (!note) return;
    setSaving(true);
    try {
      await post("/api/notes", { title: note.title, content_md: editing });
      setEditing(null);
      reload();
    } finally {
      setSaving(false);
    }
  }

  function reload() {
    get<Note>(`/api/notes/${slug}`).then(setNote).catch((e) => setError(e.message));
    get<TimelineEntry[]>(`/api/notes/${slug}/versions`).then(setTimeline).catch(() => {});
  }

  useEffect(() => {
    setNote(null); setError("");
    reload();
  }, [slug]);

  if (error) return <div className="content"><p className="muted">{error}</p><Link to="/wiki">← Back to wiki</Link></div>;
  if (!note) return <div className="content muted">Loading…</div>;

  const rail = (
    <>
      <h3 style={{ marginTop: 0 }}>Attachments</h3>
      <Attachments slug={note.slug} />

      <h3 style={{ marginTop: 20 }}>Backlinks</h3>
      {note.backlinks.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No notes link here yet.</p>}
      {note.backlinks.map((b) => (
        <Link key={b.id} to={`/note/${b.slug}`} className="list-item">{b.title}</Link>
      ))}

      <h3 style={{ marginTop: 20 }}>History</h3>
      <HistoryTimeline timeline={timeline} onView={setViewing} onDiff={(from, to) => setDiffing({ from, to })} />
    </>
  );

  const article = (
    <div className="content">
      <div className="row">
        <h1 style={{ margin: 0 }}>{note.title}</h1>
        {note.kind === "kb" && <span className="badge" style={{ marginLeft: 8 }}>KB</span>}
        <span className="spacer" />
        {editing === null && (
          <button className="ghost" onClick={() => setEditing(note.content_md)}>Edit</button>
        )}
      </div>
      <div className="muted" style={{ fontSize: 12, margin: "8px 0", display: "flex", gap: 12, flexWrap: "wrap" }}>
        <span>🕐 {note.created_at}{note.updated_at !== note.created_at ? ` · updated ${note.updated_at}` : ""}</span>
        {note.lat != null && note.lon != null && (
          <a href={`https://www.openstreetmap.org/?mlat=${note.lat}&mlon=${note.lon}#map=15/${note.lat}/${note.lon}`}
             target="_blank" rel="noreferrer">
            📍 {note.location_label || `${note.lat}, ${note.lon}`}
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
          <textarea rows={16} style={{ fontFamily: "monospace" }} value={editing}
                    onChange={(e) => setEditing(e.target.value)} />
          <div className="row" style={{ marginTop: 10 }}>
            <button className="primary" onClick={saveEdit} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
            <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
          </div>
        </div>
      ) : (
        <div className="md">
          <ReactMarkdown components={{ a: makeLinkRenderer(navigate) }}>{renderWikiLinks(note.content_md)}</ReactMarkdown>
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
