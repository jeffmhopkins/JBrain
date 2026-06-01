import { ReactNode, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get, post, put } from "../api";
import { slugify } from "../util";
import { Icon } from "../components/Icon";

interface NoteRow { slug: string; }
interface ListNote { slug: string; title: string; content_md: string; }
interface PItem { checked: boolean; text: string; priority: number | null; }
interface Parsed { header: string; items: PItem[]; queue: boolean; }

const ITEM_RE = /^(\s*)- \[( |x|X)\] (.*)$/;
const PRIO_RE = /^\(P(\d+)\)\s+(.*)$/;            // leading "(P1)" priority token
const QUEUE_MARK = "<!-- jbrain:queue -->";       // marks a priority-queue list (hidden in markdown)

// Parse a list note into a canonical model: header text + items (in display order:
// priority ascending, stable) + whether it's a priority queue.
function parseList(md: string): Parsed {
  const queue = (md || "").includes("jbrain:queue");
  const header: string[] = [];
  const items: PItem[] = [];
  let seen = false;
  for (const ln of (md || "").split("\n")) {
    const m = ln.match(ITEM_RE);
    if (m) {
      seen = true;
      let text = m[3];
      let priority: number | null = null;
      const p = text.match(PRIO_RE);
      if (p) { priority = Number(p[1]); text = p[2]; }
      items.push({ checked: m[2].toLowerCase() === "x", text, priority });
    } else if (!seen && !ln.includes("jbrain:queue")) {
      header.push(ln);
    }
  }
  items.sort((a, b) => (a.priority ?? Infinity) - (b.priority ?? Infinity));   // stable → keeps order
  return { header: header.join("\n").replace(/\n+$/, ""), items, queue };
}

// Serialize back. In queue mode every item is numbered (P1..Pn) by its position,
// so the order IS the priority; generic mode keeps any manual priority.
function serialize(p: Parsed): string {
  const out: string[] = [];
  if (p.header.trim()) out.push(p.header.replace(/\s+$/, ""));
  if (p.queue) out.push(QUEUE_MARK);
  p.items.forEach((it, i) => {
    const box = it.checked ? "[x]" : "[ ]";
    const prio = p.queue ? `(P${i + 1}) ` : (it.priority ? `(P${it.priority}) ` : "");
    out.push(`- ${box} ${prio}${it.text}`);
  });
  return out.join("\n") + "\n";
}

function renderText(text: string, progress: (slug: string) => string | null): ReactNode {
  const parts: ReactNode[] = [];
  const re = /\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]/g;
  let last = 0, m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const title = m[1].trim();
    const disp = (m[2] || m[1]).trim();
    const slug = slugify(title);
    const prog = progress(slug);
    parts.push(<Link key={m.index} to={`/note/${slug}`} className="wikilink"
                     onClick={(e) => e.stopPropagation()}>{disp.replace(/^lists\//i, "")}</Link>);
    if (prog) parts.push(<span key={m.index + "p"} className="badge" style={{ marginLeft: 6 }}>{prog}</span>);
    last = re.lastIndex;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts.length ? parts : text;
}

const leaf = (t: string) => t.replace(/^lists\//i, "");

export default function ListsPage() {
  const [lists, setLists] = useState<ListNote[]>([]);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [newName, setNewName] = useState("");

  async function load() {
    setLoading(true);
    try {
      const rows = await get<NoteRow[]>("/api/notes?kind=list");
      const full = await Promise.all(rows.map((r) => get<ListNote>(`/api/notes/${r.slug}`)));
      setLists(full.map((n) => ({ slug: n.slug, title: n.title, content_md: n.content_md })));
    } catch { /* ignore */ }
    setLoading(false);
  }
  useEffect(() => { load(); }, []);

  async function persist(list: ListNote, parsed: Parsed) {
    const content_md = serialize(parsed);
    setLists((ls) => ls.map((l) => (l.slug === list.slug ? { ...l, content_md } : l)));   // optimistic
    try { await put(`/api/notes/${list.slug}`, { title: list.title, content_md }); }
    catch { load(); }
  }
  function mutate(list: ListNote, fn: (p: Parsed) => void) {
    const p = parseList(list.content_md);
    fn(p);
    persist(list, p);
  }

  const toggle = (l: ListNote, i: number, checked: boolean) => mutate(l, (p) => { p.items[i].checked = checked; });
  const toggleQueue = (l: ListNote) => mutate(l, (p) => { p.queue = !p.queue; });
  const move = (l: ListNote, i: number, dir: -1 | 1) => mutate(l, (p) => {
    const k = i + dir;
    if (k < 0 || k >= p.items.length) return;
    [p.items[i], p.items[k]] = [p.items[k], p.items[i]];
  });
  function addItem(l: ListNote) {
    const text = (draft[l.slug] || "").trim();
    if (!text) return;
    setDraft((d) => ({ ...d, [l.slug]: "" }));
    mutate(l, (p) => { p.items.push({ checked: false, text, priority: null }); });
  }
  async function createList() {
    const name = newName.trim();
    if (!name) return;
    setNewName("");
    try { await post("/api/lists", { title: name }); load(); }
    catch (e: any) { alert(e?.message || "Couldn't create the list."); }
  }

  const bySlug = new Map(lists.map((l) => [l.slug, parseList(l.content_md).items]));
  const progress = (slug: string): string | null => {
    const its = bySlug.get(slug);
    return its && its.length ? `${its.filter((i) => i.checked).length}/${its.length}` : null;
  };

  return (
    <div className="content">
      <h2 style={{ marginTop: 0 }}>Lists</h2>
      <div className="row" style={{ gap: 6, marginBottom: 14 }}>
        <input placeholder="New list name…" value={newName}
               onChange={(e) => setNewName(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") createList(); }} />
        <button className="primary" onClick={createList}>New list</button>
      </div>

      {loading && <p className="muted">Loading…</p>}
      {!loading && lists.length === 0 && <p className="muted">No lists yet — create one above.</p>}

      {lists.map((l) => {
        const p = parseList(l.content_md);
        const done = p.items.filter((i) => i.checked).length;
        return (
          <div className="card" key={l.slug}>
            <div className="row">
              <strong>{leaf(l.title)}</strong>
              <span className="badge">{done}/{p.items.length}</span>
              <span className="spacer" />
              <button className={"ghost queue-toggle" + (p.queue ? " on" : "")} style={{ fontSize: 12 }}
                      title="Order sets numeric priority (top = P1)" onClick={() => toggleQueue(l)}>
                {p.queue ? "✓ Priority queue" : "Priority queue"}
              </button>
              <Link className="ghost" to={`/note/${l.slug}`} style={{ fontSize: 13, padding: "4px 8px" }}>Open</Link>
            </div>
            <div className="checklist">
              {p.items.map((it, i) => (
                <div key={i} className="check-row">
                  <label className={"check-item" + (it.checked ? " done" : "")}>
                    <input type="checkbox" checked={it.checked} onChange={(e) => toggle(l, i, e.target.checked)} />
                    {p.queue ? <span className="badge prio">{i + 1}</span>
                             : (it.priority != null && <span className="badge prio">P{it.priority}</span>)}
                    <span className="check-text">{renderText(it.text, progress)}</span>
                  </label>
                  <span className="reorder">
                    <button className="reorder-btn" title="Move up" disabled={i === 0}
                            onClick={() => move(l, i, -1)}><Icon name="chevron" size={14} /></button>
                    <button className="reorder-btn down" title="Move down" disabled={i === p.items.length - 1}
                            onClick={() => move(l, i, 1)}><Icon name="chevron" size={14} /></button>
                  </span>
                </div>
              ))}
              {p.items.length === 0 && <span className="muted" style={{ fontSize: 13 }}>Empty list.</span>}
            </div>
            <div className="row" style={{ marginTop: 8, gap: 6 }}>
              <input placeholder="Add an item…" value={draft[l.slug] || ""}
                     onChange={(e) => setDraft((d) => ({ ...d, [l.slug]: e.target.value }))}
                     onKeyDown={(e) => { if (e.key === "Enter") addItem(l); }} />
              <button className="ghost" onClick={() => addItem(l)}>Add</button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
