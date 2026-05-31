import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import { get, post } from "../api";
import { makeLinkRenderer } from "../util";

export interface TimelineEntry {
  version_id: number;
  is_current: boolean;
  title: string;
  source: string;
  conversation_id: number | null;
  note: string | null;
  created_at: string;
  size: number;
}

function rel(ts: string): string {
  const d = new Date(ts.replace(" ", "T") + (ts.includes("Z") ? "" : "Z"));
  return isNaN(d.getTime()) ? ts : d.toLocaleString();
}

/** Compact history list for the Note rail. */
export function HistoryTimeline({
  timeline, onView, onDiff,
}: {
  timeline: TimelineEntry[];
  onView: (v: TimelineEntry) => void;
  onDiff: (from: TimelineEntry, to: TimelineEntry) => void;
}) {
  if (timeline.length === 0) return <p className="muted" style={{ fontSize: 13 }}>No history yet.</p>;
  const current = timeline[0];
  return (
    <div>
      {timeline.map((v, i) => (
        <div key={v.version_id} className="list-item" style={{ cursor: "default" }}>
          <div className="row">
            <button className="ghost" style={{ fontSize: 12, padding: "2px 8px" }} onClick={() => onView(v)}>
              {v.is_current ? "Current" : rel(v.created_at)}
            </button>
            <span className="spacer" />
            <span className={`badge badge-${v.source}`}>{v.source}</span>
          </div>
          {!v.is_current && (
            <button className="ghost" style={{ fontSize: 11, marginTop: 6, padding: "2px 8px" }}
                    onClick={() => onDiff(v, current)}>
              Compare with current
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

/** Modal-ish overlay that views a version and offers restore. */
export function VersionViewer({
  slug, version, onClose, onRestored,
}: {
  slug: string;
  version: TimelineEntry;
  onClose: () => void;
  onRestored: () => void;
}) {
  const navigate = useNavigate();
  const [content, setContent] = useState<string>("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    get(`/api/notes/${slug}/versions/${version.version_id}`)
      .then((v) => setContent(v.content_md))
      .catch(() => setContent("(failed to load)"));
  }, [slug, version.version_id]);

  async function restore() {
    if (!confirm("Restore this version? Your current content is saved to history first, so you can undo this.")) return;
    setBusy(true);
    try {
      await post(`/api/notes/${slug}/restore`, { version_id: version.version_id });
      onRestored();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="overlay-card" onClick={(e) => e.stopPropagation()}>
        <div className="row">
          <strong>{version.is_current ? "Current version" : `Version from ${rel(version.created_at)}`}</strong>
          <span className="badge" style={{ marginLeft: 8 }}>{version.source}</span>
          <span className="spacer" />
          <button className="ghost" onClick={onClose}>Close</button>
        </div>
        <div className="md" style={{ marginTop: 12 }}>
          <ReactMarkdown components={{ a: makeLinkRenderer(navigate) }}>{content}</ReactMarkdown>
        </div>
        {!version.is_current && (
          <div className="row" style={{ marginTop: 16 }}>
            <button className="primary" onClick={restore} disabled={busy}>
              {busy ? "Restoring…" : "Restore this version"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/** Inline unified diff overlay between two versions. */
export function DiffView({
  slug, from, to, onClose,
}: {
  slug: string;
  from: TimelineEntry;
  to: TimelineEntry;
  onClose: () => void;
}) {
  const [hunks, setHunks] = useState<{ type: string; lines: string[] }[]>([]);
  const [titleChanged, setTitleChanged] = useState(false);

  useEffect(() => {
    get(`/api/notes/${slug}/diff/${from.version_id}/${to.version_id}`)
      .then((d) => { setHunks(d.hunks); setTitleChanged(d.title_changed); })
      .catch(() => setHunks([{ type: "equal", lines: ["(failed to load diff)"] }]));
  }, [slug, from.version_id, to.version_id]);

  const sym = (t: string) => (t === "insert" ? "+" : t === "delete" ? "−" : " ");
  return (
    <div className="overlay" onClick={onClose}>
      <div className="overlay-card" onClick={(e) => e.stopPropagation()}>
        <div className="row">
          <strong>Changes → current</strong>
          <span className="spacer" />
          <button className="ghost" onClick={onClose}>Close</button>
        </div>
        {titleChanged && <p className="muted">Title changed: “{from.title}” → “{to.title}”</p>}
        <pre className="diff">
          {hunks.flatMap((h, hi) =>
            h.lines.map((ln, li) => (
              <div key={`${hi}-${li}`} className={`diff-${h.type}`}>{sym(h.type)} {ln}</div>
            )),
          )}
        </pre>
      </div>
    </div>
  );
}
