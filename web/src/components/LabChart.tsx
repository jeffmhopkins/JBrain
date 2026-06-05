import { useMemo, useState } from "react";
import { LabSeries, LabPoint } from "../api";

// A dependency-free SVG trend chart for ONE analyte. Renders a STEPPED reference band
// (ranges change over time), encounter shading, a line that breaks across censored points
// and multi-year gaps, and points DUAL-ENCODED by shape AND colour (so it's readable
// without colour). Point status is whatever the server computed (flag-authoritative);
// this component never decides "normal" itself. Tap a point for its details + source note.

const VW = 1000, VH = 360;
const M = { l: 52, r: 14, t: 12, b: 34 };
const PW = VW - M.l - M.r, PH = VH - M.t - M.b;
const ms = (d: string) => Date.parse(d.length <= 10 ? d + "T00:00:00Z" : d);
const GAP_MS = 1.5 * 365 * 24 * 3600 * 1000;   // break the line across gaps longer than ~1.5y

const OUT = new Set(["high", "low", "abnormal"]);
function fill(status: string) {
  if (OUT.has(status)) return "var(--danger)";
  if (status === "normal") return "var(--ok)";
  return "var(--text-dim)";                       // unknown / no range
}

// A small status-distinct glyph at (x,y): out-of-range = triangle/square, normal = dot,
// unknown = hollow ring. Shape carries the meaning even with no colour.
function Glyph({ x, y, status, sel }: { x: number; y: number; status: string; sel: boolean }) {
  const c = fill(status);
  const r = sel ? 7 : 5;
  const ring = sel ? <circle cx={x} cy={y} r={r + 4} fill="none" stroke="var(--accent)" strokeWidth={2} /> : null;
  let shape;
  if (status === "high") shape = <path d={`M${x} ${y - r} L${x + r} ${y + r} L${x - r} ${y + r} Z`} fill={c} />;
  else if (status === "low") shape = <path d={`M${x} ${y + r} L${x + r} ${y - r} L${x - r} ${y - r} Z`} fill={c} />;
  else if (status === "abnormal") shape = <rect x={x - r} y={y - r} width={r * 2} height={r * 2} fill={c} />;
  else if (status === "unknown") shape = <circle cx={x} cy={y} r={r} fill="var(--bg-elev)" stroke={c} strokeWidth={2} />;
  else shape = <circle cx={x} cy={y} r={r} fill={c} />;
  return <>{ring}{shape}</>;
}

export default function LabChart({ series, from, to, height, onPick }: {
  series: LabSeries; from?: string; to?: string; height?: number;
  onPick?: (p: LabPoint | null) => void;
}) {
  const [sel, setSel] = useState<number | null>(null);
  const pts = series.points;

  const view = useMemo(() => {
    const lo = from ? ms(from) : (series.domain ? ms(series.domain.from) : 0);
    const hi = to ? ms(to) : (series.domain ? ms(series.domain.to) : 1);
    const span = Math.max(1, hi - lo);
    // y from numeric points in view + reference-band bounds, padded 8%.
    const vals: number[] = [];
    for (const p of pts) if (p.v != null && ms(p.t) >= lo && ms(p.t) <= hi) vals.push(p.v);
    for (const s of series.segments) {
      if (ms(s.to) < lo || ms(s.from) > hi) continue;
      if (s.low != null) vals.push(s.low);
      if (s.high != null) vals.push(s.high);
    }
    let ymin = vals.length ? Math.min(...vals) : 0, ymax = vals.length ? Math.max(...vals) : 1;
    if (ymin === ymax) { ymin -= 1; ymax += 1; }
    const pad = (ymax - ymin) * 0.08; ymin -= pad; ymax += pad;
    const x = (t: string) => M.l + ((ms(t) - lo) / span) * PW;
    const y = (v: number) => M.t + (1 - (v - ymin) / (ymax - ymin)) * PH;
    const clampX = (t: number) => M.l + (Math.min(hi, Math.max(lo, t)) - lo) / span * PW;
    return { lo, hi, ymin, ymax, x, y, clampX };
  }, [series, from, to, pts]);

  const { lo, hi, ymin, ymax, x, y, clampX } = view;
  const inView = (t: string) => ms(t) >= lo && ms(t) <= hi;

  // Year gridlines/ticks across the visible domain.
  const years: number[] = [];
  for (let yr = new Date(lo).getUTCFullYear(); yr <= new Date(hi).getUTCFullYear(); yr++) years.push(yr);
  // A few value ticks.
  const yticks = [0, 0.25, 0.5, 0.75, 1].map((f) => ymin + f * (ymax - ymin));

  // Line runs: split the polyline at censored points and long gaps so we never draw a
  // line through a value we don't have or across a multi-year void.
  const runs: LabPoint[][] = [];
  let run: LabPoint[] = [];
  for (const p of pts) {
    if (!inView(p.t)) continue;
    if (p.censored || p.v == null) { if (run.length) runs.push(run); run = []; continue; }
    if (run.length && ms(p.t) - ms(run[run.length - 1].t) > GAP_MS) { runs.push(run); run = []; }
    run.push(p);
  }
  if (run.length) runs.push(run);

  function pick(i: number | null) { setSel(i); onPick?.(i == null ? null : pts[i]); }
  const selP = sel != null ? pts[sel] : null;

  return (
    <svg viewBox={`0 0 ${VW} ${VH}`} width="100%" height={height ?? undefined}
         style={{ touchAction: "pan-y" }} role="img"
         aria-label={`${series.test_name} trend chart`} onClick={() => pick(null)}>
      {/* out-of-range wash, then the green in-range band painted on top per segment */}
      <rect x={M.l} y={M.t} width={PW} height={PH} fill="var(--danger)" fillOpacity={0.06} />
      {series.encounters.map((e) => {
        const x0 = clampX(ms(e.from)), x1 = clampX(e.to ? ms(e.to) : hi);
        if (x1 <= x0) return null;
        return <rect key={e.id} x={x0} y={M.t} width={x1 - x0} height={PH}
                     fill="var(--accent)" fillOpacity={0.13}><title>{e.label}</title></rect>;
      })}
      {series.segments.map((s, i) => {
        const x0 = clampX(ms(s.from)), x1 = clampX(ms(s.to));
        const yTop = s.high != null ? y(s.high) : M.t;
        const yBot = s.low != null ? y(s.low) : M.t + PH;
        if (x1 < x0) return null;
        return <rect key={i} x={x0} y={yTop} width={Math.max(0, x1 - x0)} height={Math.max(0, yBot - yTop)}
                     fill="var(--ok)" fillOpacity={0.18} />;
      })}
      {/* y grid + labels */}
      {yticks.map((v, i) => (
        <g key={i}>
          <line x1={M.l} y1={y(v)} x2={M.l + PW} y2={y(v)} stroke="var(--border)" strokeWidth={1} />
          <text x={M.l - 6} y={y(v) + 3} textAnchor="end" fontSize={13} fill="var(--text-dim)">{v.toFixed(1)}</text>
        </g>
      ))}
      {/* x year ticks */}
      {years.map((yr) => {
        const xx = clampX(Date.UTC(yr, 0, 1));
        return <g key={yr}>
          <line x1={xx} y1={M.t} x2={xx} y2={M.t + PH} stroke="var(--border)" strokeWidth={1} strokeDasharray="2 4" />
          <text x={xx} y={VH - 12} textAnchor="middle" fontSize={13} fill="var(--text-dim)">{yr}</text>
        </g>;
      })}
      {/* line runs */}
      {runs.map((r, i) => (
        <polyline key={i} fill="none" stroke="var(--accent)" strokeWidth={2}
                  points={r.map((p) => `${x(p.t)},${y(p.v as number)}`).join(" ")} />
      ))}
      {/* censored markers at the foot of the plot */}
      {pts.map((p, i) => p.censored && inView(p.t) ? (
        <text key={`c${i}`} x={x(p.t)} y={M.t + PH - 2} textAnchor="middle" fontSize={13}
              fill="var(--text-dim)" onClick={(ev) => { ev.stopPropagation(); pick(i); }}
              style={{ cursor: "pointer" }}>⌄</text>
      ) : null)}
      {/* points */}
      {pts.map((p, i) => !p.censored && p.v != null && inView(p.t) ? (
        <g key={i} onClick={(ev) => { ev.stopPropagation(); pick(i); }} style={{ cursor: "pointer" }}>
          {/* generous invisible hit target for touch */}
          <circle cx={x(p.t)} cy={y(p.v)} r={14} fill="transparent" />
          <Glyph x={x(p.t)} y={y(p.v)} status={p.status} sel={sel === i} />
        </g>
      ) : null)}
      {/* selected-point tooltip (in-SVG so it scales) */}
      {selP && selP.v != null && (() => {
        const tx = Math.min(M.l + PW - 196, Math.max(M.l, x(selP.t) - 96));
        return (
          <g transform={`translate(${tx} ${M.t + 6})`} pointerEvents="none">
            <rect width={192} height={58} rx={8} fill="var(--bg-elev-2)" stroke="var(--border)" />
            <text x={10} y={20} fontSize={14} fill="var(--text)" fontWeight={600}>
              {selP.vtext}{selP.unit ? " " + selP.unit : ""} · {selP.status}
            </text>
            <text x={10} y={38} fontSize={12} fill="var(--text-dim)">{selP.t}
              {selP.ref_text ? `  (ref ${selP.ref_text})` : ""}</text>
            <text x={10} y={52} fontSize={12} fill="var(--accent)">{selP.note_title || ""}</text>
          </g>
        );
      })()}
    </svg>
  );
}
