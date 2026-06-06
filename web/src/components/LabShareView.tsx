import { useEffect, useMemo, useState } from "react";
import { labsStart, getShareLabSeries, LabSeries, LabPoint } from "../api";
import LabChart from "./LabChart";

// Recipient view of a data-only lab-share link (kind='labs'): a fixed selection of results the
// owner chose to share, viewed the SAME way as the owner's Labs page — pick a result and a single
// chart updates (time-window presets, click a point for its value/range). No chatbot (the scoped
// AI lives in assisted/research links). ALL series come through the token-scoped, allow-list-
// rechecked, no-store endpoint — already clamped to the owner's window, so local zoom can't pan
// past it. Nothing here is identity-bearing (no names, no notes, no source links).

const OUT = new Set(["high", "low", "abnormal"]);
const ms = (d: string) => Date.parse(d.length <= 10 ? d + "T00:00:00Z" : d);
const iso = (t: number) => new Date(t).toISOString().slice(0, 10);
// Keep the SAME time window when switching results (parity with LabsPage), unless it'd be empty.
function keepWin(w: { from: string; to: string } | null, s: LabSeries) {
  if (!s.domain) return w;
  if (!w) return { from: s.domain.from, to: s.domain.to };
  return s.points.some((p) => p.t >= w.from && p.t <= w.to) ? w : { from: s.domain.from, to: s.domain.to };
}
const PRESETS: { label: string; years: number | null }[] = [
  { label: "1y", years: 1 }, { label: "5y", years: 5 }, { label: "All", years: null },
];

// A small coloured/shaped status pip — dual-encoded so it reads without colour.
function Pip({ status }: { status: string }) {
  const c = OUT.has(status) ? "var(--danger)" : status === "normal" ? "var(--ok)" : "var(--text-dim)";
  const ch = status === "high" ? "▲" : status === "low" ? "▼" : status === "normal" ? "●" : "○";
  return <span style={{ color: c, fontSize: 11 }} title={status}>{ch}</span>;
}

type Analyte = { analyte: string; test_name: string; unit: string | null };

export default function LabShareView({ token, brainName, intro, consent }: {
  token: string; brainName: string; intro?: string; consent?: string;
}) {
  const [phase, setPhase] = useState<"consent" | "viewing">("consent");
  const [name, setName] = useState(localStorage.getItem("jbrain_share_name") || "");
  const [analytes, setAnalytes] = useState<Analyte[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [series, setSeries] = useState<LabSeries | null>(null);
  const [win, setWin] = useState<{ from: string; to: string } | null>(null);
  const [picked, setPicked] = useState<LabPoint | null>(null);
  const [showTable, setShowTable] = useState(false);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // Load the selected result's (windowed) series; keep the time window across switches.
  useEffect(() => {
    if (!sel) return;
    setSeries(null); setPicked(null); setErr("");
    getShareLabSeries(token, sel).then((s) => { setSeries(s); setWin((w) => keepWin(w, s)); })
      .catch(() => setErr("Couldn’t load that result."));
  }, [sel, token]);

  const filtered = useMemo(() => {
    const f = q.trim().toLowerCase();
    return analytes.filter((a) => !f || a.test_name.toLowerCase().includes(f) || a.analyte.includes(f));
  }, [analytes, q]);

  async function begin() {
    if (!name.trim()) { alert("Please enter your name."); return; }
    localStorage.setItem("jbrain_share_name", name.trim());
    setBusy(true); setErr("");
    try {
      const r = await labsStart(token, name.trim());
      const list: Analyte[] = r.analytes || [];
      setAnalytes(list);
      setSel(list.length ? list[0].analyte : null);
      setPhase("viewing");
    } catch (e: any) { setErr(e?.message || "Couldn't open this link."); }
    finally { setBusy(false); }
  }

  function preset(years: number | null) {
    if (!series?.domain) return;
    const to = ms(series.domain.to);
    const from = years == null ? ms(series.domain.from)
      : Math.max(ms(series.domain.from), to - years * 365 * 24 * 3600 * 1000);
    setWin({ from: iso(from), to: iso(to) });
  }

  if (phase === "consent") return (
    <div className="share-page"><div className="share-card">
      <div className="share-head">
        <span className="brand">{brainName}<span className="dot">.</span></span>
        <span className="badge">Shared · Lab results</span>
      </div>
      <h2 style={{ marginTop: 12 }}>{brainName}’s shared lab results</h2>
      {intro && <p>{intro}</p>}
      <div className="share-banner">⚕️ Not a diagnosis. Some values may have been read from a photo —
        always confirm against the original lab reports. This is a fixed selection, not a full record,
        and may not be current.</div>
      {consent && <p className="muted" style={{ fontSize: 13 }}>{consent}</p>}
      <input placeholder="Your name *" value={name} onChange={(e) => setName(e.target.value)}
             onKeyDown={(e) => { if (e.key === "Enter") begin(); }} />
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={busy} onClick={begin}>{busy ? "…" : "View results"}</button>
      </div>
      {err && <p className="share-error">{err}</p>}
    </div></div>
  );

  const cur = analytes.find((a) => a.analyte === sel);
  return (
    <div className="tool-body">
      <div className="guided-head" style={{ marginBottom: 8 }}>
        <span className="brand">{brainName}<span className="dot">.</span></span>
        <span className="badge">Shared · Lab results</span>
      </div>
      <div className="share-banner">⚕️ Not a diagnosis — always confirm against the source reports.
        Some values may have been read from a photo.</div>

      {analytes.length === 0 && <div className="muted" style={{ fontSize: 13 }}>No results to show.</div>}

      {analytes.length > 1 && (
        <input placeholder="Find a result…" value={q} onChange={(e) => setQ(e.target.value)}
               style={{ marginBottom: 8 }} />
      )}
      {analytes.length > 1 && (
        <div className="lab-picker">
          {filtered.map((a) => (
            <button key={a.analyte} className={"lab-pick" + (a.analyte === sel ? " active" : "")}
                    onClick={() => setSel(a.analyte)}>
              <span className="lab-pick-name">{a.test_name}</span>
              {a.unit && <span className="muted" style={{ fontSize: 11 }}>{a.unit}</span>}
            </button>
          ))}
          {filtered.length === 0 && <div className="muted" style={{ padding: 8, fontSize: 13 }}>No results match.</div>}
        </div>
      )}

      {err && <p className="share-error">{err}</p>}

      {cur && series && win && (
        <div className="lab-chart-wrap">
          <div className="lab-head">
            <strong>{series.test_name}</strong>
            {series.unit && <span className="muted" style={{ fontSize: 12 }}> ({series.unit})</span>}
            {series.other_units.length > 0 &&
              <span className="muted" style={{ fontSize: 11 }}> · also recorded in {series.other_units.join(", ")}</span>}
          </div>

          {series.points.length === 0 ? (
            <p className="muted" style={{ fontSize: 13 }}>No plottable values for this result.</p>
          ) : series.points.filter((p) => !p.censored).length < 2 ? (
            <p className="muted" style={{ fontSize: 13 }}>Only one result on {series.points[0]?.t} — a trend appears with a second draw.</p>
          ) : (
            <>
              <div className="lab-controls">
                {PRESETS.map((p) => <button key={p.label} className="chip" onClick={() => preset(p.years)}>{p.label}</button>)}
                <input type="date" value={win.from} min={series.domain?.from} max={win.to}
                       onChange={(e) => setWin({ ...win, from: e.target.value })} />
                <input type="date" value={win.to} min={win.from} max={series.domain?.to}
                       onChange={(e) => setWin({ ...win, to: e.target.value })} />
                <button className="chip" onClick={() => series.domain && setWin({ ...series.domain })}>Reset</button>
              </div>
              <LabChart series={series} from={win.from} to={win.to} onPick={setPicked}
                        onViewChange={(f, t) => setWin({ from: f, to: t })} />
              {picked && (
                <div className="lab-detail">
                  <Pip status={picked.status} /> <strong>{picked.vtext}{picked.unit ? " " + picked.unit : ""}</strong>
                  <span className="muted"> on {picked.t}{picked.ref_text ? ` · ref ${picked.ref_text}` : ""}
                    {picked.flag ? ` · lab flag ${picked.flag}` : ""}</span>
                </div>
              )}
            </>
          )}

          {/* Accessible / colour-blind / offline fallback: the same data as a table. Controlled so
              it stays open when you switch to another result. No source column (identity-stripped). */}
          <details className="lab-table" open={showTable}
                   onToggle={(e) => setShowTable((e.target as HTMLDetailsElement).open)}>
            <summary className="muted" style={{ fontSize: 12 }}>Show as table</summary>
            <table>
              <thead><tr><th>Date</th><th>Value</th><th>Range</th><th>Status</th></tr></thead>
              <tbody>
                {series.points.map((p, i) => (
                  <tr key={i}>
                    <td>{p.t}</td><td>{p.vtext}{p.unit ? " " + p.unit : ""}</td>
                    <td className="muted">{p.ref_text || "—"}</td>
                    <td><Pip status={p.status} /> {p.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </div>
      )}
    </div>
  );
}
