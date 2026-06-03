// One renderer for every kind of staged change so approvals look + read the same
// everywhere (chat staging, Shares proposals). The server supplies a uniform preview:
//   { kind: "text"|"fields"|"place"|"tags", before, after, label, stale?, conflict? }
// `before`/`after` are the actual states apply will move between — see what's changing.

export interface Preview {
  kind: "text" | "fields" | "place" | "tags";
  before: any;
  after: any;
  label?: string;
  stale?: boolean;
  conflict?: string;
}

// Cheap line-level diff (set difference of non-empty trimmed lines).
export function lineDiff(cur: string, prop: string) {
  const a = new Set((cur || "").split("\n").map((s) => s.trim()).filter(Boolean));
  const b = new Set((prop || "").split("\n").map((s) => s.trim()).filter(Boolean));
  return { removed: [...a].filter((x) => !b.has(x)), added: [...b].filter((x) => !a.has(x)) };
}

// A compact one-line summary of the change, for the collapsed card header.
export function previewStat(p?: Preview | null): string {
  if (!p) return "";
  if (p.kind === "text") {
    if (p.after == null) return "deletes this";
    if (p.before == null) return "new";
    const d = lineDiff(p.before || "", p.after || "");
    return d.added.length || d.removed.length ? `+${d.added.length}/−${d.removed.length}` : "no textual change";
  }
  if (p.kind === "tags") {
    const before = new Set<string>(p.before || []), after = new Set<string>(p.after || []);
    const add = [...after].filter((x) => !before.has(x)).length;
    const rem = [...before].filter((x) => !after.has(x)).length;
    return `+${add}/−${rem} tag${add + rem === 1 ? "" : "s"}`;
  }
  // fields / place: count changed keys
  const keys = new Set([...Object.keys(p.before || {}), ...Object.keys(p.after || {})]);
  const changed = [...keys].filter((k) => String((p.before || {})[k] ?? "") !== String((p.after || {})[k] ?? "")).length;
  return p.before == null ? "new" : `${changed} field${changed === 1 ? "" : ""} changed`;
}

function FieldTable({ before, after }: { before: any; after: any }) {
  const keys = [...new Set([...Object.keys(before || {}), ...Object.keys(after || {})])];
  return (
    <table className="approval-fields">
      <tbody>
        {keys.map((k) => {
          const b = (before || {})[k], a = (after || {})[k];
          const changed = String(b ?? "") !== String(a ?? "");
          return (
            <tr key={k} className={changed ? "changed" : ""}>
              <th>{k}</th>
              <td className="was">{before == null ? <span className="muted">—</span> : String(b ?? "—")}</td>
              <td className="arrow">→</td>
              <td className="now">{after == null ? <span className="muted">—</span> : String(a ?? "—")}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export default function ApprovalView({ preview, full }: { preview: Preview; full?: boolean }) {
  if (!preview) return null;
  const p = preview;

  if (p.kind === "text") {
    const isNew = p.before == null;
    const isDelete = p.after == null;
    if (isNew) return <pre className="share-diff">{p.after}</pre>;
    if (isDelete) return (
      <>
        <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>This note will be deleted (soft, undoable):</div>
        <pre className="share-diff deleting">{p.before}</pre>
      </>
    );
    const d = lineDiff(p.before || "", p.after || "");
    return (
      <>
        <div className="share-diff">
          {d.removed.length === 0 && d.added.length === 0 && <span className="muted">No textual change.</span>}
          {d.removed.map((l, i) => <div key={"r" + i} style={{ color: "var(--danger)" }}>− {l}</div>)}
          {d.added.map((l, i) => <div key={"x" + i} style={{ color: "#4ade80" }}>+ {l}</div>)}
        </div>
        {full && (
          <details style={{ marginTop: 6 }}>
            <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>Full proposed content</summary>
            <pre className="share-diff" style={{ marginTop: 6 }}>{p.after}</pre>
          </details>
        )}
      </>
    );
  }

  if (p.kind === "tags") {
    const before = new Set<string>(p.before || []), after = new Set<string>(p.after || []);
    const all = [...new Set<string>([...(p.before || []), ...(p.after || [])])].sort();
    return (
      <div className="approval-tags">
        {all.map((t) => {
          const inB = before.has(t), inA = after.has(t);
          const cls = inA && !inB ? "added" : !inA && inB ? "removed" : "kept";
          return <span key={t} className={"tag-chip " + cls}>{!inA && inB ? "−" : inA && !inB ? "+" : ""} {t}</span>;
        })}
        {all.length === 0 && <span className="muted">(no tags)</span>}
      </div>
    );
  }

  // fields / place
  return <FieldTable before={p.before} after={p.after} />;
}
