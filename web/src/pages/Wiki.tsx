import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../api";
import { Icon } from "../components/Icon";

interface NoteRow { id: number; title: string; slug: string; kind: string; updated_at: string; }
type Filter = "" | "entry" | "kb";
type TNode = { name: string; note?: NoteRow; children: Record<string, TNode> };

const TABS: { key: Filter; label: string }[] = [
  { key: "", label: "All" },
  { key: "entry", label: "Entries" },
  { key: "kb", label: "Knowledge base" },
];

// Build a tree from "/"-separated titles (e.g. "Work/Q3 Planning").
function buildTree(notes: NoteRow[]): TNode {
  const root: TNode = { name: "", children: {} };
  for (const n of notes) {
    const parts = n.title.split("/").map((s) => s.trim()).filter(Boolean);
    let cur = root;
    (parts.length ? parts : [n.title]).forEach((seg, i, arr) => {
      cur.children[seg] = cur.children[seg] || { name: seg, children: {} };
      cur = cur.children[seg];
      if (i === arr.length - 1) cur.note = n;
    });
  }
  return root;
}

export default function Wiki() {
  const [notes, setNotes] = useState<NoteRow[]>([]);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<Filter>("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  useEffect(() => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (kind) params.set("kind", kind);
    const qs = params.toString();
    get<NoteRow[]>(`/api/notes${qs ? `?${qs}` : ""}`).then(setNotes).catch(() => {});
  }, [q, kind]);

  const tree = useMemo(() => buildTree(notes), [notes]);

  function toggle(path: string) {
    setCollapsed((s) => {
      const next = new Set(s);
      next.has(path) ? next.delete(path) : next.add(path);
      return next;
    });
  }

  function renderNodes(nodes: TNode[], depth: number, prefix: string): JSX.Element[] {
    return [...nodes]
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((node) => {
        const path = prefix ? `${prefix}/${node.name}` : node.name;
        const kids = Object.values(node.children);
        const open = !collapsed.has(path);
        return (
          <div key={path}>
            <div className="tree-row" style={{ paddingLeft: 6 + depth * 16 }}>
              {kids.length > 0 ? (
                <button className="tree-toggle" onClick={() => toggle(path)} aria-label="toggle">
                  <span className={"chev" + (open ? " open" : "")}><Icon name="chevron" size={14} /></span>
                </button>
              ) : <span className="tree-spacer" />}
              {node.note ? (
                <Link to={`/note/${node.note.slug}`} className="tree-label">
                  {node.name}
                  {node.note.kind === "kb" && <span className="badge" style={{ marginLeft: 6 }}>KB</span>}
                </Link>
              ) : (
                <span className="tree-label folder" onClick={() => kids.length && toggle(path)}>{node.name}</span>
              )}
            </div>
            {kids.length > 0 && open && renderNodes(kids, depth + 1, path)}
          </div>
        );
      });
  }

  const top = Object.values(tree.children);

  return (
    <div className="tool">
      <div className="tool-bar">
        {TABS.map((t) => (
          <button key={t.key} className={kind === t.key ? "primary" : "ghost"} onClick={() => setKind(t.key)}>{t.label}</button>
        ))}
        <input className="tool-filter" placeholder="Filter notes by title…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="tool-body">
        {top.length === 0 && <p className="muted">Nothing here yet.</p>}
        {renderNodes(top, 0, "")}
        <p className="muted" style={{ fontSize: 12, marginTop: 16 }}>
          Tip: name a note like <code>Work/Q3 Planning</code> to nest it under <code>Work</code>.
        </p>
      </div>
    </div>
  );
}
