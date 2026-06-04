import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { get } from "../api";
import { Icon } from "../components/Icon";

type Mode = "hybrid" | "keyword" | "semantic";
interface Result {
  kind: "note" | "attachment" | "entity";
  title?: string;
  slug?: string;
  score: number;
  distance?: number;   // vector distance, present on semantic hits
  filename?: string;
  snippet?: string;
  attachments?: number;   // count of files on a note hit (deterministic clip badge)
  // entity hits (name/alias match over the canonical entity index):
  name?: string;
  entity_type?: string;
  note_count?: number;
  article_title?: string | null;
}

// Same glyphs as the Entities browser, so a hit reads consistently across the app.
const ENTITY_ICON: Record<string, string> = {
  person: "👤", animal: "🐾", org: "🏢", place: "📍", thing: "📦", work: "🎬",
  condition: "🩺", medication: "💊", procedure: "🩻", event: "📅", concept: "💡",
};

// Embeddings are unit-normalised, so the vec0 L2 distance maps to cosine
// similarity: sim = 1 - d²/2. Clamp and show as a 0–100% relevance weight.
function weightPct(distance: number): number {
  const sim = 1 - (distance * distance) / 2;
  return Math.round(Math.max(0, Math.min(1, sim)) * 100);
}

const MODES: Mode[] = ["hybrid", "keyword", "semantic"];

export default function SearchPage() {
  // Seed from the URL so the search survives navigating into a note and back
  // (the page unmounts on navigation; the URL is what restores it).
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState(() => params.get("q") || "");
  const [mode, setMode] = useState<Mode>(() =>
    MODES.includes(params.get("mode") as Mode) ? (params.get("mode") as Mode) : "hybrid");
  const [results, setResults] = useState<Result[]>([]);
  const [searched, setSearched] = useState(false);
  const reqId = useRef(0);

  // Mirror the query into the URL (replace, so typing doesn't spam history) so
  // Back from a note returns to a populated search.
  useEffect(() => {
    const next = new URLSearchParams();
    if (q.trim()) next.set("q", q.trim());
    if (mode !== "hybrid") next.set("mode", mode);
    setParams(next, { replace: true });
  }, [q, mode]);   // eslint-disable-line react-hooks/exhaustive-deps

  // A 0–100% relevance badge per result. Semantic = true cosine similarity from
  // the vector distance. Hybrid blends keyword + semantic via a rank-fusion score
  // that has no absolute scale, so show it normalised to the top hit (best = 100%).
  // Keyword (bm25 fusion) has no meaningful absolute weight, so no badge.
  const maxScore = useMemo(() => results.reduce((m, r) => Math.max(m, r.score || 0), 0), [results]);
  const weightOf = (r: Result): number | null => {
    if (mode === "semantic") return r.distance != null ? weightPct(r.distance) : null;
    if (mode === "hybrid") return maxScore > 0 ? Math.round((r.score / maxScore) * 100) : null;
    return null;
  };

  // Search as you type: debounce input, and re-run when the mode changes too.
  // A request id guards against out-of-order responses overwriting newer ones.
  useEffect(() => {
    const query = q.trim();
    if (!query) { setResults([]); setSearched(false); return; }
    const id = ++reqId.current;
    const t = window.setTimeout(async () => {
      try {
        const r = await get<Result[]>(`/api/search?q=${encodeURIComponent(query)}&mode=${mode}`);
        if (id === reqId.current) { setResults(r); setSearched(true); }
      } catch { /* ignore transient errors while typing */ }
    }, 200);
    return () => window.clearTimeout(t);
  }, [q, mode]);

  // Enter just suppresses a page reload — results are already live.
  function onSubmit(e: FormEvent) { e.preventDefault(); }

  return (
    <div className="content">
      <h2>Search</h2>
      <form onSubmit={onSubmit}>
        <input placeholder="Search by keyword or meaning…" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
        <div className="row" style={{ marginTop: 10 }}>
          {(["hybrid", "keyword", "semantic"] as Mode[]).map((m) => (
            <button type="button" key={m} className={mode === m ? "primary" : "ghost"} onClick={() => setMode(m)}>
              {m}
            </button>
          ))}
        </div>
      </form>
      <div style={{ marginTop: 18 }}>
        {searched && results.length === 0 && <p className="muted">No results.</p>}
        {results.map((r) => {
          const w = weightOf(r);
          // Entities link to their browse view (notes + KB article); notes/attachments to the note.
          const to = r.kind === "entity"
            ? `/entities?q=${encodeURIComponent(r.name || "")}`
            : `/note/${r.slug}`;
          const title = r.kind === "entity" ? r.name : r.title;
          return (
            <Link key={`${r.kind}:${r.slug ?? r.name}:${r.filename ?? ""}`} to={to} className="list-item">
              <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
                <div style={{ fontWeight: 600 }}>
                  {r.kind === "entity" && <span style={{ marginRight: 6 }}>{ENTITY_ICON[r.entity_type || ""] || "•"}</span>}
                  {title}
                </div>
                {r.kind === "note" && !!r.attachments && (
                  <span className="muted" style={{ fontSize: 12 }} title={`${r.attachments} attachment${r.attachments === 1 ? "" : "s"}`}>
                    <Icon name="clip" size={12} />{r.attachments > 1 ? ` ${r.attachments}` : ""}
                  </span>
                )}
                {w != null && <span className="search-weight">{w}%</span>}
              </div>
              {r.kind === "entity" && (
                <div className="muted" style={{ fontSize: 12 }}>
                  entity · {r.entity_type}{r.note_count != null ? ` · ${r.note_count} note${r.note_count === 1 ? "" : "s"}` : ""}
                  {r.article_title ? " · has a KB article" : ""}
                </div>
              )}
              {r.kind === "attachment" && (
                <div className="muted" style={{ fontSize: 12 }}>
                  <Icon name="clip" size={13} /> in {r.filename}{r.snippet ? ` — ${r.snippet}` : ""}
                </div>
              )}
            </Link>
          );
        })}
      </div>
    </div>
  );
}
