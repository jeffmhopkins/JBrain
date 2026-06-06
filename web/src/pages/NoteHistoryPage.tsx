import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { get } from "../api";
import { leaf } from "../util";
import { DiffView, HistoryTimeline, TimelineEntry, VersionViewer } from "../components/VersionViewer";

// Full revision history for a note, on its own page — so the note page itself only
// shows the last couple of edits and doesn't grow an endless tail of versions.
// Reuses the same view / compare / restore flow (VersionViewer + DiffView).
export default function NoteHistoryPage() {
  const { slug } = useParams();
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [title, setTitle] = useState("");
  const [loading, setLoading] = useState(true);
  const [viewing, setViewing] = useState<TimelineEntry | null>(null);
  const [diffing, setDiffing] = useState<{ from: TimelineEntry; to: TimelineEntry } | null>(null);

  function load() {
    if (!slug) return;
    get<TimelineEntry[]>(`/api/notes/${slug}/versions`).then(setTimeline).catch(() => {}).finally(() => setLoading(false));
    get<{ title: string }>(`/api/notes/${slug}`).then((n) => setTitle(n.title)).catch(() => {});
  }
  useEffect(load, [slug]);

  return (
    <div className="content">
      <h2 style={{ marginTop: 0, marginBottom: 4 }}>Version history</h2>
      {title && (
        <p className="muted" style={{ marginTop: 0 }}>
          <Link to={`/note/${slug}`} className="wikilink">{leaf(title)}</Link>
          {timeline.length > 0 && ` · ${timeline.length} version${timeline.length === 1 ? "" : "s"}`}
        </p>
      )}
      {loading
        ? <p className="muted">Loading…</p>
        : <HistoryTimeline timeline={timeline} onView={setViewing} onDiff={(from, to) => setDiffing({ from, to })} />}

      {viewing && slug && (
        <VersionViewer slug={slug} version={viewing}
          onClose={() => setViewing(null)}
          onRestored={() => { setViewing(null); setLoading(true); load(); }} />
      )}
      {diffing && slug && (
        <DiffView slug={slug} from={diffing.from} to={diffing.to} onClose={() => setDiffing(null)} />
      )}
    </div>
  );
}
