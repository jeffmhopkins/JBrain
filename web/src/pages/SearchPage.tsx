import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../api";
import { Icon } from "../components/Icon";

type Mode = "hybrid" | "keyword" | "semantic";
interface Result {
  kind: "note" | "attachment";
  title: string;
  slug: string;
  score: number;
  filename?: string;
  snippet?: string;
}

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<Mode>("hybrid");
  const [results, setResults] = useState<Result[]>([]);
  const [searched, setSearched] = useState(false);

  async function run(e: FormEvent) {
    e.preventDefault();
    if (!q.trim()) return;
    const r = await get<Result[]>(`/api/search?q=${encodeURIComponent(q)}&mode=${mode}`);
    setResults(r);
    setSearched(true);
  }

  return (
    <div className="content">
      <h2>Search</h2>
      <form onSubmit={run}>
        <input placeholder="Search by keyword or meaning…" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
        <div className="row" style={{ marginTop: 10 }}>
          {(["hybrid", "keyword", "semantic"] as Mode[]).map((m) => (
            <button type="button" key={m} className={mode === m ? "primary" : "ghost"} onClick={() => setMode(m)}>
              {m}
            </button>
          ))}
          <div className="spacer" />
          <button className="primary" type="submit">Search</button>
        </div>
      </form>
      <div style={{ marginTop: 18 }}>
        {searched && results.length === 0 && <p className="muted">No results.</p>}
        {results.map((r, i) => (
          <Link key={i} to={`/note/${r.slug}`} className="list-item">
            <div style={{ fontWeight: 600 }}>{r.title}</div>
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
