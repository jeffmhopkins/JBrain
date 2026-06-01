import { ReactNode, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { del, get, post, put } from "../api";
import { slugify } from "../util";
import ListEditor from "../components/ListEditor";
import { Parsed, parseList, serialize } from "../lists";

interface NoteRow { slug: string; }
interface ListNote { slug: string; title: string; content_md: string; }

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
  const [newName, setNewName] = useState("");
  const stripRef = useRef<HTMLDivElement>(null);
  const pendingFocus = useRef<string | null>(null);   // slug to scroll to after create
  const scrollHideTimer = useRef<number>();

  // Reveal the carousel scrollbar only while scrolling, then auto-hide it (so it
  // isn't a persistent bar on first load). Mirrors the chat .messages behavior.
  function onStripScroll() {
    const el = stripRef.current;
    if (!el) return;
    el.classList.add("scrolling");
    clearTimeout(scrollHideTimer.current);
    scrollHideTimer.current = window.setTimeout(() => el.classList.remove("scrolling"), 900);
  }

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

  // The Lists card persists every edit immediately.
  async function persist(list: ListNote, parsed: Parsed) {
    const content_md = serialize(parsed);
    setLists((ls) => ls.map((l) => (l.slug === list.slug ? { ...l, content_md } : l)));   // optimistic
    try { await put(`/api/notes/${list.slug}`, { title: list.title, content_md }); }
    catch { load(); }
  }
  async function removeList(l: ListNote) {
    if (!confirm(`Delete the list “${leaf(l.title)}”? (soft-deleted, restorable)`)) return;
    setLists((ls) => ls.filter((x) => x.slug !== l.slug));
    try { await del(`/api/notes/${l.slug}`); } catch { load(); }
  }
  async function createList() {
    const name = newName.trim();
    if (!name) return;
    setNewName("");
    try {
      const created = await post<{ slug: string }>("/api/lists", { title: name });
      pendingFocus.current = created.slug;   // jump the carousel to it once it loads
      await load();
    } catch (e: any) { alert(e?.message || "Couldn't create the list."); }
  }

  // After a (re)load, snap the carousel to a just-created list.
  useEffect(() => {
    const s = pendingFocus.current;
    if (!s || !stripRef.current) return;
    const i = lists.findIndex((l) => l.slug === s);
    if (i >= 0) {
      (stripRef.current.children[i] as HTMLElement | undefined)?.scrollIntoView({ inline: "center", behavior: "smooth" });
      pendingFocus.current = null;
    }
  }, [lists]);

  const bySlug = new Map(lists.map((l) => [l.slug, parseList(l.content_md).items]));
  const progress = (slug: string): string | null => {
    const its = bySlug.get(slug);
    return its && its.length ? `${its.filter((i) => i.checked).length}/${its.length}` : null;
  };

  return (
    <div className="content">
      <div className="row" style={{ gap: 6, marginBottom: 14 }}>
        <input placeholder="New list name…" value={newName}
               onChange={(e) => setNewName(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") createList(); }} />
        <button className="primary" onClick={createList}>New list</button>
      </div>

      {loading && <p className="muted">Loading…</p>}
      {!loading && lists.length === 0 && <p className="muted">No lists yet — create one above.</p>}

      {/* Horizontal carousel: one list per screen, swipe (or trackpad/scrollbar)
          to move between them. Native scroll-snap — no gesture code, and it leaves
          vertical scroll + Shell's vertical swipe to .ubody. */}
      {lists.length > 0 && (
        <div className="list-strip" ref={stripRef} onScroll={onStripScroll}>
          {lists.map((l) => {
            const p = parseList(l.content_md);
            const done = p.items.filter((i) => i.checked).length;
            return (
              <div className="card" key={l.slug}>
                <div className="row">
                  <strong>{leaf(l.title)}</strong>
                  <span className="badge">{done}/{p.items.length}</span>
                  <span className="spacer" />
                  <Link className="ghost" to={`/note/${l.slug}`} style={{ fontSize: 13, padding: "4px 8px" }}>Open</Link>
                  <button className="ghost danger-hover" style={{ fontSize: 13, padding: "4px 8px" }} onClick={() => removeList(l)}>Delete</button>
                </div>
                <ListEditor value={p} onChange={(next) => persist(l, next)}
                            renderItemText={(t) => renderText(t, progress)} />
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
