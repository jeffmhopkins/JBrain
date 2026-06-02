import { FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../api";
import { Icon } from "../components/Icon";

type Mode = "hybrid" | "keyword" | "semantic";
interface Result {
  kind: "note" | "attachment";
  title: string;
  slug: string;
  score: number;
  distance?: number;   // vector distance, present on semantic hits
  filename?: string;
  snippet?: string;
}

// Embeddings are unit-normalised, so the vec0 L2 distance maps to cosine
// similarity: sim = 1 - d²/2. Clamp and show as a 0–100% relevance weight.
function weightPct(distance: number): number {
  const sim = 1 - (distance * distance) / 2;
  return Math.round(Math.max(0, Math.min(1, sim)) * 100);
}

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<Mode>("hybrid");
  const [results, setResults] = useState<Result[]>([]);
  const [searched, setSearched] = useState(false);
  const reqId = useRef(0);

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
        {results.map((r, i) => (
          <Link key={i} to={`/note/${r.slug}`} className="list-item">
            <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
              <div style={{ fontWeight: 600 }}>{r.title}</div>
              {mode === "semantic" && r.distance != null && (
                <span className="search-weight">{weightPct(r.distance)}%</span>
              )}
            </div>
            {r.kind === "attachment" && (
              <div className="muted" style={{ fontSize: 12 }}>
                <Icon name="clip" size={13} /> in {r.filename}{r.snippet ? ` — ${r.snippet}` : ""}
              </div>
            )}
          </Link>
        ))}
      </div>
    </div>
  );
}
