import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { get, refreshNoteAnalysis } from "../api";
import { markReplaceNav } from "../nav";
import { Icon } from "./Icon";
import { expandTimeTokens } from "../time";

interface Entity { type: string; name: string; }
interface Analysis {
  gist?: string;
  facts?: string[];
  entities?: Entity[];
  dates?: string[];
  domain?: string | null;
  analyzed_at?: string;
  slug?: string;
}

const ENTITY_ICON: Record<string, string> = {
  person: "👤", animal: "🐾", org: "🏢", place: "📍", thing: "📦", work: "🎬",
  condition: "🩺", medication: "💊", procedure: "🩻", event: "📅", concept: "💡",
};

// A note's AI analysis sidecar (gist, salient facts, entities, domain), read-only — it never
// edits the note body. The ↻ button force-recomputes it (ignoring the cache) AND runs the
// title check (a bare dated note gets a generated title), so an un-analyzed note can be
// analyzed on the spot. Always shows the control once loaded, even with no analysis yet.
export default function AiAnalysisPanel({ slug }: { slug: string }) {
  const [a, setA] = useState<Analysis | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    let live = true;
    get<Analysis>(`/api/notes/${slug}/analysis`)
      .then((r) => { if (live) { setA(r && (r.gist || r.facts?.length) ? r : null); setLoaded(true); } })
      .catch(() => { if (live) { setA(null); setLoaded(true); } });
    return () => { live = false; };
  }, [slug]);

  async function reanalyze() {
    setBusy(true);
    try {
      const r = await refreshNoteAnalysis(slug);
      // The title check may have renamed the note (new slug) — follow it to the fresh URL.
      // Mark it a replace so Shell's back-stack swaps the top instead of stacking the dead
      // old slug (otherwise back would land on the pre-rename note → 404).
      if (r?.slug && r.slug !== slug) { markReplaceNav(); navigate(`/note/${r.slug}`, { replace: true }); return; }
      setA(r && (r.gist || r.facts?.length || r.entities?.length) ? r : null);
    } catch { /* keep what we have */ }
    finally { setBusy(false); }
  }

  if (!loaded) return null;   // wait for the initial read so an analyzed note doesn't flash empty

  return (
    <div style={{ marginTop: 20 }}>
      <div className="row" style={{ alignItems: "center", gap: 8, marginBottom: 6 }}>
        <h3 style={{ margin: 0 }}>AI analysis</h3>
        {a?.domain && a.domain !== "Unsure" && (
          <span className="badge" style={{ verticalAlign: "middle", fontWeight: 500 }}>{a.domain}</span>
        )}
        <span style={{ flex: 1 }} />
        <button className="icon-btn" style={{ padding: 4 }} disabled={busy} onClick={reanalyze}
                title={busy ? "Analyzing…" : a ? "Re-analyze + title this note (ignores the cache)"
                                               : "Analyze + title this note"}>
          <Icon name="refresh" size={15} />
        </button>
      </div>
      <p className="muted" style={{ fontSize: 11, marginTop: 0 }}>
        {busy ? "Analyzing…"
              : a ? "Auto-extracted, read-only. The note itself is unchanged."
                  : "Not analyzed yet — generate the AI sidecar (gist, facts, entities) and a title."}
      </p>

      {a && (
        <>
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
        </>
      )}
    </div>
  );
}
