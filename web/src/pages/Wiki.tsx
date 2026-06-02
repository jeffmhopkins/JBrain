import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../api";
import { Icon } from "../components/Icon";

interface NoteRow { id: number; title: string; slug: string; kind: string; updated_at: string; }
type Filter = "" | "entry" | "kb" | "list";
type TNode = { name: string; note?: NoteRow; children: Record<string, TNode> };

const TABS: { key: Filter; label: string }[] = [
  { key: "", label: "All" },
  { key: "entry", label: "Entries" },
  { key: "list", label: "Lists" },
  { key: "kb", label: "Knowledge base" },
];
const KIND_BADGE: Record<string, string> = { kb: "KB", list: "List" };

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
  const [kind, setKind] = useState<Filter>("kb");   // default to the knowledge base
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  useEffect(() => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (kind) params.set("kind", kind);
    const qs = params.toString();
    get<NoteRow[]>(`/api/notes${qs ? `?${qs}` : ""}`).then(setNotes).catch(() => {});
  }, [q, kind]);

  const tree = useMemo(() => buildTree(notes), [notes]);

  // On load (and whenever the visible set changes), start with every folder
  // collapsed EXCEPT the branch where the most recent note lives — so you open
  // focused on where you last added something, not a fully-expanded wall.
  useEffect(() => {
    if (notes.length === 0) { setCollapsed(new Set()); return; }
    const norm = (t: string) => t.split("/").map((s) => s.trim()).filter(Boolean);
    const recent = notes.reduce((a, b) => (a.updated_at >= b.updated_at ? a : b));
    // Folder paths along the recent note's branch — keep these open.
    const keepOpen = new Set<string>();
    let acc = "";
    for (const seg of norm(recent.title)) { acc = acc ? `${acc}/${seg}` : seg; keepOpen.add(acc); }
    // Every collapsible folder = any strict ancestor prefix across all titles.
    const next = new Set<string>();
    for (const n of notes) {
      const parts = norm(n.title);
      let p = "";
      for (let i = 0; i < parts.length - 1; i++) {
        p = p ? `${p}/${parts[i]}` : parts[i];
        if (!keepOpen.has(p)) next.add(p);
      }
    }
    setCollapsed(next);
  }, [notes]);

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
                  {KIND_BADGE[node.note.kind] && <span className="badge" style={{ marginLeft: 6 }}>{KIND_BADGE[node.note.kind]}</span>}
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
