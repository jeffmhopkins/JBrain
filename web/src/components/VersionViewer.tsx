import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { get, post } from "../api";
import { makeLinkRenderer, renderWikiLinks, stripSummarySentinels } from "../util";
import MarkdownDiff from "./MarkdownDiff";
import Modal from "./Modal";

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

/** Compact history list for the Note rail.
 *  `limit` caps how many entries render inline; when more exist and `seeAllHref`
 *  is given, a "See full history" link is shown instead of a runaway list. */
export function HistoryTimeline({
  timeline, onView, onDiff, limit, seeAllHref,
}: {
  timeline: TimelineEntry[];
  onView: (v: TimelineEntry) => void;
  onDiff: (from: TimelineEntry, to: TimelineEntry) => void;
  limit?: number;
  seeAllHref?: string;
}) {
  if (timeline.length === 0) return <p className="muted" style={{ fontSize: 13 }}>No history yet.</p>;
  const current = timeline[0];
  const shown = limit != null ? timeline.slice(0, limit) : timeline;
  const hidden = timeline.length - shown.length;
  return (
    <div>
      {shown.map((v, i) => (
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
      {seeAllHref && hidden > 0 && (
        <Link to={seeAllHref} className="ghost" style={{ display: "inline-block", fontSize: 12, marginTop: 8, padding: "4px 10px" }}>
          See full history ({timeline.length}) →
        </Link>
      )}
    </div>
  );
}

/** Modal that views a version and offers restore. */
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
    <Modal
      title={version.is_current ? "Current version" : `Version from ${rel(version.created_at)}`}
      headerExtra={<span className="badge">{version.source}</span>}
      onClose={onClose}
      footer={!version.is_current
        ? <button className="primary" onClick={restore} disabled={busy}>{busy ? "Restoring…" : "Restore this version"}</button>
        : undefined}
    >
      <div className="md">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: makeLinkRenderer(navigate) }}>{renderWikiLinks(stripSummarySentinels(content))}</ReactMarkdown>
      </div>
    </Modal>
  );
}

/** Unified diff modal between two versions. */
export function DiffView({
  slug, from, to, onClose,
}: {
  slug: string;
  from: TimelineEntry;
  to: TimelineEntry;
  onClose: () => void;
}) {
  const [content, setContent] = useState<{ before: string; after: string } | null>(null);
  const [titleChanged, setTitleChanged] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    get(`/api/notes/${slug}/diff/${from.version_id}/${to.version_id}`)
      .then((d) => { setContent({ before: d.before ?? "", after: d.after ?? "" }); setTitleChanged(d.title_changed); })
      .catch(() => setFailed(true));
  }, [slug, from.version_id, to.version_id]);

  return (
    <Modal title="Changes → current" onClose={onClose}>
      {titleChanged && <p className="muted">Title changed: “{from.title}” → “{to.title}”</p>}
      {failed && <p className="muted">(failed to load diff)</p>}
      {content && <MarkdownDiff before={content.before} after={content.after} />}
    </Modal>
  );
}
