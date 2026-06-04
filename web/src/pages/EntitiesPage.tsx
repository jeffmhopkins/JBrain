import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { get } from "../api";
import { leaf, slugify } from "../util";

interface Entity { id: number; type: string; canonical_name: string; note_count: number; article_title: string | null; }
interface EntityDetail extends Entity { aliases?: string[]; notes: { id: number; title: string; slug: string; created_at: string }[]; }

const ICON: Record<string, string> = {
  person: "👤", org: "🏢", place: "📍", thing: "📦",
  condition: "🩺", medication: "💊", procedure: "🩻", event: "📅", concept: "💡",
};
const TYPES = [["", "All"], ["person", "People"], ["org", "Orgs"], ["place", "Places"],
  ["thing", "Things"], ["condition", "Conditions"], ["medication", "Meds"], ["procedure", "Procedures"]];

// Browse the canonical entity index aggregated from per-note AI analysis. Reached from
// the analysis-panel chips (?q=&type=) or directly; clicking an entity shows every note
// that mentions it, plus its KB article if one exists.
export default function EntitiesPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") || "";
  const type = params.get("type") || "";
  const [list, setList] = useState<Entity[]>([]);
  const [sel, setSel] = useState<EntityDetail | null>(null);

  useEffect(() => {
    const qs = new URLSearchParams();
    if (q) qs.set("q", q);
    if (type) qs.set("type", type);
    get<Entity[]>(`/api/entities?${qs}`).then(setList).catch(() => setList([]));
  }, [q, type]);

  // If the query names exactly one entity (e.g. arriving from a chip), open it.
  useEffect(() => {
    if (q && list.length >= 1 && (!sel || sel.canonical_name.toLowerCase() !== q.toLowerCase())) {
      const hit = list.find((e) => e.canonical_name.toLowerCase() === q.toLowerCase()) || (list.length === 1 ? list[0] : null);
      if (hit) open(hit.id);
    }
  }, [list]); // eslint-disable-line

  function open(id: number) { get<EntityDetail>(`/api/entities/${id}`).then(setSel).catch(() => {}); }
  function setType(t: string) { const p = new URLSearchParams(params); t ? p.set("type", t) : p.delete("type"); setParams(p); }
  function setQ(v: string) { const p = new URLSearchParams(params); v ? p.set("q", v) : p.delete("q"); setParams(p); }

  return (
    <div className="content">
      <h2 style={{ marginTop: 0 }}>Entities</h2>
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>
        People, organizations, places, and things the AI found across your notes.
      </p>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", margin: "10px 0" }}>
        <input className="modal-input" placeholder="Search entities…" value={q}
               onChange={(e) => setQ(e.target.value)} style={{ flex: "1 1 200px" }} />
        {TYPES.map(([val, label]) => (
          <button key={val} className={"ghost" + (type === val ? " active" : "")} onClick={() => setType(val)}>{label}</button>
        ))}
      </div>

      <div style={{ display: "flex", gap: 24, flexWrap: "wrap", alignItems: "flex-start" }}>
        <ul style={{ listStyle: "none", padding: 0, flex: "1 1 280px", maxWidth: 420 }}>
          {list.map((e) => (
            <li key={e.id}>
              <button className="entity-row" onClick={() => open(e.id)}>
                <span>{ICON[e.type] || "•"} {e.canonical_name}</span>
                <span className="muted" style={{ fontSize: 12 }}>{e.note_count}</span>
              </button>
            </li>
          ))}
          {list.length === 0 && <li className="muted" style={{ fontSize: 13 }}>No entities yet — run Note analysis / a rebuild first.</li>}
        </ul>

        {sel && (
          <div style={{ flex: "1 1 320px" }}>
            <h3 style={{ marginTop: 0 }}>{ICON[sel.type] || "•"} {sel.canonical_name}</h3>
            {!!sel.aliases?.length && <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>a.k.a. {sel.aliases.join(", ")}</p>}
            {sel.article_title
              ? <p><Link to={`/note/${slugify(sel.article_title)}`} className="wikilink">📖 {leaf(sel.article_title)}</Link></p>
              : <p className="muted" style={{ fontSize: 13 }}>No KB article yet.</p>}
            <h4>Mentioned in {sel.notes.length} note{sel.notes.length === 1 ? "" : "s"}</h4>
            <ul style={{ paddingLeft: 18 }}>
              {sel.notes.map((n) => (
                <li key={n.id}><Link to={`/note/${n.slug}`}>{leaf(n.title)}</Link></li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
