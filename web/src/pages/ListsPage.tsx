import { ReactNode, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get, put } from "../api";
import { slugify } from "../util";
import { Icon } from "../components/Icon";

interface NoteRow { id: number; title: string; slug: string; kind: string; }
interface ListNote { slug: string; title: string; content_md: string; }
interface Item { idx: number; checked: boolean; text: string; priority: number | null; }

const ITEM_RE = /^(\s*)- \[( |x|X)\] (.*)$/;
const PRIO_RE = /^\(P(\d+)\)\s+(.*)$/;   // leading priority token, e.g. "(P1) buy milk"

// Parse checkbox lines from a list note's markdown. Keeps the original line index
// (for toggling) and sorts for display: priority ascending (1 = highest), then
// original order; unprioritized items sink to the bottom but keep their order.
function parseItems(md: string): Item[] {
  const items: Item[] = [];
  md.split("\n").forEach((ln, idx) => {
    const m = ln.match(ITEM_RE);
    if (!m) return;
    let text = m[3];
    let priority: number | null = null;
    const p = text.match(PRIO_RE);
    if (p) { priority = Number(p[1]); text = p[2]; }
    items.push({ idx, checked: m[2].toLowerCase() === "x", text, priority });
  });
  return items
    .map((it, i) => ({ it, i }))
    .sort((a, b) => (a.it.priority ?? Infinity) - (b.it.priority ?? Infinity) || a.i - b.i)
    .map((x) => x.it);
}

function setLineChecked(md: string, idx: number, checked: boolean): string {
  const lines = md.split("\n");
  lines[idx] = lines[idx].replace(/- \[( |x|X)\]/, checked ? "- [x]" : "- [ ]");
  return lines.join("\n");
}

// Render item text, turning [[wiki-links]] (e.g. sub-list references) into links,
// and appending a "done/total" badge for referenced sub-lists.
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
    parts.push(
      <Link key={m.index} to={`/note/${slug}`} className="wikilink"
            onClick={(e) => e.stopPropagation()}>{disp.replace(/^lists\//i, "")}</Link>,
    );
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

  async function persist(list: ListNote, content_md: string) {
    setLists((ls) => ls.map((l) => (l.slug === list.slug ? { ...l, content_md } : l)));   // optimistic
    try {
      await put(`/api/notes/${list.slug}`, { title: list.title, content_md });
    } catch {
      load();   // on failure, re-sync from the server
    }
  }

  function toggle(list: ListNote, idx: number, checked: boolean) {
    persist(list, setLineChecked(list.content_md, idx, checked));
  }
  // Reorder: swap the source lines of two display-adjacent items (j and j+dir),
  // so moving works within a priority tier (and freely on unprioritised lists).
  function move(list: ListNote, displayed: Item[], j: number, dir: -1 | 1) {
    const k = j + dir;
    if (k < 0 || k >= displayed.length) return;
    const lines = list.content_md.split("\n");
    const a = displayed[j].idx, b = displayed[k].idx;
    [lines[a], lines[b]] = [lines[b], lines[a]];
    persist(list, lines.join("\n"));
  }

  function addItem(list: ListNote) {
    const text = (draft[list.slug] || "").trim();
    if (!text) return;
    setDraft((d) => ({ ...d, [list.slug]: "" }));
    persist(list, list.content_md.replace(/\s*$/, "") + `\n- [ ] ${text}\n`);
  }

  // done/total for a referenced sub-list (by slug), computed from the fetched lists.
  const bySlug = new Map(lists.map((l) => [l.slug, parseItems(l.content_md)]));
  const progress = (slug: string): string | null => {
    const its = bySlug.get(slug);
    return its && its.length ? `${its.filter((i) => i.checked).length}/${its.length}` : null;
  };

  return (
    <div className="content">
      <h2 style={{ marginTop: 0 }}>Lists</h2>
      {loading && <p className="muted">Loading…</p>}
      {!loading && lists.length === 0 && (
        <p className="muted">No lists yet. In Assisted mode, try “add milk to the shopping list.”</p>
      )}
      {lists.map((l) => {
        const items = parseItems(l.content_md);
        const done = items.filter((i) => i.checked).length;
        return (
          <div className="card" key={l.slug}>
            <div className="row">
              <strong>{leaf(l.title)}</strong>
              <span className="badge">{done}/{items.length}</span>
              <span className="spacer" />
              <Link className="ghost" to={`/note/${l.slug}`} style={{ fontSize: 13, padding: "4px 8px" }}>Open</Link>
            </div>
            <div className="checklist">
              {items.map((it, j) => (
                <div key={it.idx} className="check-row">
                  <label className={"check-item" + (it.checked ? " done" : "")}>
                    <input type="checkbox" checked={it.checked}
                           onChange={(e) => toggle(l, it.idx, e.target.checked)} />
                    {it.priority != null && <span className="badge prio">P{it.priority}</span>}
                    <span className="check-text">{renderText(it.text, progress)}</span>
                  </label>
                  <span className="reorder">
                    <button className="reorder-btn" title="Move up" disabled={j === 0}
                            onClick={() => move(l, items, j, -1)}><Icon name="chevron" size={14} /></button>
                    <button className="reorder-btn down" title="Move down" disabled={j === items.length - 1}
                            onClick={() => move(l, items, j, 1)}><Icon name="chevron" size={14} /></button>
                  </span>
                </div>
              ))}
              {items.length === 0 && <span className="muted" style={{ fontSize: 13 }}>Empty list.</span>}
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
