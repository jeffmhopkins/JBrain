import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../api";

interface NoteRow { id: number; title: string; slug: string; updated_at: string; }

export default function Wiki() {
  const [notes, setNotes] = useState<NoteRow[]>([]);
  const [q, setQ] = useState("");

  useEffect(() => {
    get<NoteRow[]>(`/api/notes${q ? `?q=${encodeURIComponent(q)}` : ""}`).then(setNotes).catch(() => {});
  }, [q]);

  return (
    <div className="content">
      <h2>Wiki</h2>
      <input placeholder="Filter notes by title…" value={q} onChange={(e) => setQ(e.target.value)} />
      <div style={{ marginTop: 16 }}>
        {notes.length === 0 && <p className="muted">No notes yet. Head to Chat and start a conversation.</p>}
        {notes.map((n) => (
          <Link key={n.id} to={`/note/${n.slug}`} className="list-item">
            <div style={{ fontWeight: 600 }}>{n.title}</div>
            <div className="muted" style={{ fontSize: 12 }}>updated {n.updated_at}</div>
          </Link>
        ))}
      </div>
    </div>
  );
}
