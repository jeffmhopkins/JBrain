import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../api";

interface NoteRow { id: number; title: string; slug: string; kind: string; updated_at: string; }
type Filter = "" | "entry" | "kb";

const TABS: { key: Filter; label: string }[] = [
  { key: "", label: "All" },
  { key: "entry", label: "Entries" },
  { key: "kb", label: "Knowledge base" },
];

export default function Wiki() {
  const [notes, setNotes] = useState<NoteRow[]>([]);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<Filter>("");

  useEffect(() => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (kind) params.set("kind", kind);
    const qs = params.toString();
    get<NoteRow[]>(`/api/notes${qs ? `?${qs}` : ""}`).then(setNotes).catch(() => {});
  }, [q, kind]);

  return (
    <div className="content">
      <h2>Wiki</h2>
      <div className="row" style={{ gap: 6, marginBottom: 10 }}>
        {TABS.map((t) => (
          <button key={t.key} className={kind === t.key ? "primary" : "ghost"} onClick={() => setKind(t.key)}>
            {t.label}
          </button>
        ))}
      </div>
      <input placeholder="Filter notes by title…" value={q} onChange={(e) => setQ(e.target.value)} />
      <div style={{ marginTop: 16 }}>
        {notes.length === 0 && <p className="muted">Nothing here yet.</p>}
        {notes.map((n) => (
          <Link key={n.id} to={`/note/${n.slug}`} className="list-item">
            <div style={{ fontWeight: 600 }}>
              {n.title}
              {n.kind === "kb" && <span className="badge" style={{ marginLeft: 8 }}>KB</span>}
            </div>
            <div className="muted" style={{ fontSize: 12 }}>updated {n.updated_at}</div>
          </Link>
        ))}
      </div>
    </div>
  );
}
