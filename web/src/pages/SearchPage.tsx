import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../api";

type Mode = "hybrid" | "keyword" | "semantic";
interface Result { id: number; title: string; slug: string; score: number; }

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
        {results.map((r) => (
          <Link key={r.id} to={`/note/${r.slug}`} className="list-item">{r.title}</Link>
        ))}
      </div>
    </div>
  );
}
