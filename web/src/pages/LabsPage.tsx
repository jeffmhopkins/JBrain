import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { LabAnalyte, LabPoint, LabSeries, getLabAnalytes, getLabSeries, getPendingLabs } from "../api";
import LabChart from "../components/LabChart";
import { Icon } from "../components/Icon";

const OUT = new Set(["high", "low", "abnormal"]);
const ms = (d: string) => Date.parse(d.length <= 10 ? d + "T00:00:00Z" : d);
const iso = (t: number) => new Date(t).toISOString().slice(0, 10);
// Keep the SAME time window when switching analytes, so flipping through labs doesn't move
// the view. Keep the exact [from,to] whenever this analyte has any results inside it; only
// fall back to its full range when the window would otherwise be empty (or on first load).
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

export default function LabsPage() {
  const [analytes, setAnalytes] = useState<LabAnalyte[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [series, setSeries] = useState<LabSeries | null>(null);
  const [q, setQ] = useState("");
  const [win, setWin] = useState<{ from: string; to: string } | null>(null);
  const [picked, setPicked] = useState<LabPoint | null>(null);
  const [showTable, setShowTable] = useState(false);   // persists across analyte switches
  const [pending, setPending] = useState<{ note_slug: string; note_title: string }[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => { getPendingLabs().then((r) => setPending(r.pending)).catch(() => {}); }, []);
  useEffect(() => {
    getLabAnalytes().then(({ analytes }) => {
      setAnalytes(analytes);
      if (analytes.length && !sel) setSel(analytes[0].analyte);
    }).catch(() => setErr("Couldn't load labs."));
  }, []);

  useEffect(() => {
    if (!sel) return;
    setSeries(null); setPicked(null);
    getLabSeries(sel).then((s) => {
      setSeries(s);
      setWin((w) => keepWin(w, s));   // keep the exact time window across analyte switches
    }).catch(() => setErr("Couldn't load that analyte."));
  }, [sel]);

  // Abnormal-most-recent first, then by name — so the eye lands on what's off.
  const sorted = useMemo(() => {
    const f = q.trim().toLowerCase();
    return analytes
      .filter((a) => !f || a.test_name.toLowerCase().includes(f) || a.analyte.includes(f))
      .sort((a, b) => (OUT.has(b.last_status) ? 1 : 0) - (OUT.has(a.last_status) ? 1 : 0)
        || b.last_at.localeCompare(a.last_at) || a.test_name.localeCompare(b.test_name));
  }, [analytes, q]);

  function preset(years: number | null) {
    if (!series?.domain) return;
    const to = ms(series.domain.to);
    const from = years == null ? ms(series.domain.from) : Math.max(ms(series.domain.from), to - years * 365 * 24 * 3600 * 1000);
    setWin({ from: iso(from), to: iso(to) });
  }

  const banner = pending.length > 0 && (
    <div className="lab-pending-banner">
      <Icon name="medical" size={15} />
      <span style={{ flex: 1 }}>{pending.length} lab import{pending.length > 1 ? "s" : ""} awaiting review.</span>
      <Link to={`/note/${pending[0].note_slug}`} className="lab-src">Review →</Link>
    </div>
  );

  if (err && !analytes.length) return <div className="tool-body"><p className="muted">{err}</p></div>;
  if (!analytes.length) return (
    <div className="tool-body">
      {banner}
      <p className="muted" style={{ fontSize: 13 }}>{pending.length ? "Approve a lab import to see trends here."
        : <>No lab results yet. Upload a lab PDF in <strong>Medical</strong> mode and approve it to see trends.</>}</p>
    </div>
  );

  const cur = analytes.find((a) => a.analyte === sel);
  return (
    <div className="tool-body">
      {banner}
      <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
        Your own results over time, straight from the documents you uploaded. <strong>Not a diagnostic
        tool</strong> — always verify against your source reports; each point links to its.
      </p>

      <input placeholder="Find a result…" value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 8 }} />
      <div className="lab-picker">
        {sorted.map((a) => (
          <button key={a.analyte} className={"lab-pick" + (a.analyte === sel ? " active" : "")}
                  onClick={() => setSel(a.analyte)}>
            <Pip status={a.last_status} />
            <span className="lab-pick-name">{a.test_name}</span>
            <span className="muted" style={{ fontSize: 11 }}>{a.last_value}{a.unit ? " " + a.unit : ""} · {a.n}</span>
          </button>
        ))}
      </div>

      {cur && series && win && (
        <div className="lab-chart-wrap">
          <div className="lab-head">
            <strong>{series.test_name}</strong>
            {series.unit && <span className="muted" style={{ fontSize: 12 }}> ({series.unit})</span>}
            {series.other_units.length > 0 &&
              <span className="muted" style={{ fontSize: 11 }}> · also recorded in {series.other_units.join(", ")}</span>}
          </div>

          {series.points.length === 0 ? (
            <p className="muted" style={{ fontSize: 13 }}>No plottable values for this analyte.</p>
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
                  {picked.note_slug && <Link to={`/note/${picked.note_slug}`} className="lab-src">View source →</Link>}
                </div>
              )}
            </>
          )}

          {/* Accessible / colour-blind / offline fallback: the same data as a table. The
              open state is controlled so it stays open when you switch to another analyte. */}
          <details className="lab-table" open={showTable}
                   onToggle={(e) => setShowTable((e.target as HTMLDetailsElement).open)}>
            <summary className="muted" style={{ fontSize: 12 }}>Show as table</summary>
            <table>
              <thead><tr><th>Date</th><th>Value</th><th>Range</th><th>Status</th><th></th></tr></thead>
              <tbody>
                {series.points.map((p, i) => (
                  <tr key={i}>
                    <td>{p.t}</td><td>{p.vtext}{p.unit ? " " + p.unit : ""}</td>
                    <td className="muted">{p.ref_text || "—"}</td>
                    <td><Pip status={p.status} /> {p.status}</td>
                    <td>{p.note_slug && <Link to={`/note/${p.note_slug}`}>source</Link>}</td>
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
