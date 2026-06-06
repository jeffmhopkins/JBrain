import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { makeLinkRenderer, renderWikiLinks, stripSummarySentinels } from "../util";
import { buildMarkdownDiff, rehypeDiff } from "../diff";

// Shared "rendered diff" — shows the note with FULL markdown formatting and the
// change highlighted in place (green = added, red strikethrough = removed). Used
// by every approval/proposal surface so they look + read identically.
//
//   before == null → brand-new note  (whole body shown as an addition)
//   after  == null → deletion         (whole body shown struck-through)
//   otherwise       → in-place diff
export default function MarkdownDiff({
  before, after, className,
}: {
  before: string | null | undefined;
  after: string | null | undefined;
  className?: string;
}) {
  const navigate = useNavigate();
  const isNew = before == null;
  const isDelete = after == null;

  const { src, mode, changed } = useMemo(() => {
    if (isNew) return { src: after || "", mode: "new" as const, changed: true };
    if (isDelete) return { src: before || "", mode: "del" as const, changed: true };
    const r = buildMarkdownDiff(before || "", after || "");
    return { src: r.merged, mode: "diff" as const, changed: r.changed };
  }, [before, after, isNew, isDelete]);

  const body = renderWikiLinks(stripSummarySentinels(src));
  const cls = [
    "md", "md-diff",
    mode === "new" ? "jb-all-add" : mode === "del" ? "jb-all-del" : "",
    className || "",
  ].filter(Boolean).join(" ");

  return (
    <div className={cls}>
      {mode === "diff" && !changed && (
        <div className="muted" style={{ fontSize: 12 }}>No textual change.</div>
      )}
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={mode === "diff" ? [rehypeDiff as any] : []}
        components={{ a: makeLinkRenderer(navigate) }}
      >{body}</ReactMarkdown>
    </div>
  );
}
