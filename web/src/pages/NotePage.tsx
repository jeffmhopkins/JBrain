import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { get } from "../api";
import { useIsDesktop } from "../hooks";
import { renderWikiLinks } from "../util";

interface Note {
  id: number; title: string; slug: string; content_md: string; updated_at: string;
  backlinks: { id: number; title: string; slug: string }[];
  tags: string[];
}
interface Version { id: number; content_md: string; created_at: string; }

export default function NotePage() {
  const { slug } = useParams();
  const navigate = useNavigate();
  const isDesktop = useIsDesktop();
  const [note, setNote] = useState<Note | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    setNote(null); setError("");
    get<Note>(`/api/notes/${slug}`).then(setNote).catch((e) => setError(e.message));
    get<Version[]>(`/api/notes/${slug}/versions`).then(setVersions).catch(() => {});
  }, [slug]);

  if (error) return <div className="content"><p className="muted">{error}</p><Link to="/wiki">← Back to wiki</Link></div>;
  if (!note) return <div className="content muted">Loading…</div>;

  // Internal links navigate via the router; external open normally.
  const linkRenderer = ({ href, children }: any) => {
    if (href?.startsWith("/note/")) {
      return <a className="wikilink" onClick={(e) => { e.preventDefault(); navigate(href); }} href={href}>{children}</a>;
    }
    return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
  };

  const article = (
    <div className="content">
      <h1>{note.title}</h1>
      {note.tags.length > 0 && (
        <div className="row" style={{ gap: 6, marginBottom: 8 }}>
          {note.tags.map((t) => <span key={t} className="badge">#{t}</span>)}
        </div>
      )}
      <div className="md">
        <ReactMarkdown components={{ a: linkRenderer }}>{renderWikiLinks(note.content_md)}</ReactMarkdown>
      </div>
      {!isDesktop && <Rail note={note} versions={versions} />}
    </div>
  );

  if (!isDesktop) return article;
  return (
    <div className="with-rail">
      {article}
      <aside className="rail"><Rail note={note} versions={versions} /></aside>
    </div>
  );
}

function Rail({ note, versions }: { note: Note; versions: Version[] }) {
  return (
    <>
      <h3>Backlinks</h3>
      {note.backlinks.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No notes link here yet.</p>}
      {note.backlinks.map((b) => (
        <Link key={b.id} to={`/note/${b.slug}`} className="list-item">{b.title}</Link>
      ))}
      <h3 style={{ marginTop: 20 }}>History</h3>
      <p className="muted" style={{ fontSize: 13 }}>{versions.length} previous version(s).</p>
    </>
  );
}
