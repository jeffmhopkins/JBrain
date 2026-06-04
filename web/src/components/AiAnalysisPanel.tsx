import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { get } from "../api";
import { expandTimeTokens } from "../time";

interface Entity { type: string; name: string; }
interface Analysis {
  gist?: string;
  facts?: string[];
  entities?: Entity[];
  dates?: string[];
  domain?: string | null;
  analyzed_at?: string;
}

const ENTITY_ICON: Record<string, string> = {
  person: "👤", org: "🏢", place: "📍", thing: "📦",
  condition: "🩺", medication: "💊", procedure: "🩻", event: "📅", concept: "💡",
};

// Read-only view of a note's cached AI analysis sidecar (gist, salient facts,
// entities, domain). It never edits the note — the analysis lives beside it. Renders
// nothing until an analysis exists, so notes that haven't been analyzed stay clean.
export default function AiAnalysisPanel({ slug }: { slug: string }) {
  const [a, setA] = useState<Analysis | null>(null);

  useEffect(() => {
    let live = true;
    get<Analysis>(`/api/notes/${slug}/analysis`)
      .then((r) => { if (live) setA(r && (r.gist || r.facts?.length) ? r : null); })
      .catch(() => { if (live) setA(null); });
    return () => { live = false; };
  }, [slug]);

  if (!a) return null;

  return (
    <div style={{ marginTop: 20 }}>
      <h3 style={{ marginBottom: 6 }}>
        AI analysis
        {a.domain && a.domain !== "Unsure" && (
          <span className="badge" style={{ marginLeft: 8, verticalAlign: "middle", fontWeight: 500 }}>{a.domain}</span>
        )}
      </h3>
      <p className="muted" style={{ fontSize: 11, marginTop: 0 }}>
        Auto-extracted, read-only. The note itself is unchanged.
      </p>

      {a.gist && <p style={{ fontSize: 13, fontStyle: "italic", margin: "6px 0" }}>{expandTimeTokens(a.gist)}</p>}

      {!!a.facts?.length && (
        <ul style={{ fontSize: 13, paddingLeft: 18, margin: "6px 0" }}>
          {a.facts.map((f, i) => <li key={i}>{expandTimeTokens(f)}</li>)}
        </ul>
      )}

      {!!a.entities?.length && (
        <div className="backlink-row" style={{ marginTop: 6 }}>
          {a.entities.map((e, i) => (
            <Link key={i} className="backlink-chip" title={`See all notes about ${e.name}`}
                  to={`/entities?type=${encodeURIComponent(e.type)}&q=${encodeURIComponent(e.name)}`}>
              {ENTITY_ICON[e.type] || "•"} {e.name}
            </Link>
          ))}
        </div>
      )}

      {!!a.dates?.length && (
        <ul className="muted" style={{ fontSize: 12, paddingLeft: 18, margin: "6px 0 0" }}>
          {a.dates.map((d, i) => <li key={i}>{d}</li>)}
        </ul>
      )}
    </div>
  );
}
