import { useEffect, useId, useMemo, useRef, useState } from "react";
import { LabSeries, LabPoint } from "../api";

// A dependency-free SVG trend chart for ONE analyte. Renders a STEPPED reference band
// (ranges change over time), encounter shading, a line that breaks across censored points
// and multi-year gaps, and points DUAL-ENCODED by shape AND colour (so it's readable
// without colour). Point status is whatever the server computed (flag-authoritative);
// this component never decides "normal" itself. Tap a point for details; drag to pan,
// pinch to zoom (vertical scroll passes through); double-tap to reset.

const VW = 1000, VH = 360;
const M = { l: 52, r: 14, t: 12, b: 34 };
const PW = VW - M.l - M.r, PH = VH - M.t - M.b;
const DAY = 24 * 3600 * 1000, YR = 365 * DAY;
const MIN_SPAN = 7 * DAY;
const GAP_MS = 1.5 * YR;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const ms = (d: string) => Date.parse(d.length <= 10 ? d + "T00:00:00Z" : d);
const isoOf = (t: number) => new Date(t).toISOString().slice(0, 10);

const OUT = new Set(["high", "low", "abnormal"]);
function fill(status: string) {
  if (OUT.has(status)) return "var(--danger)";
  if (status === "normal") return "var(--ok)";
  return "var(--text-dim)";                       // unknown / no range
}

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

// Adaptive x ticks: years when wide, months mid-range, and DAYS once zoomed in past ~3
// months (labelled "Mon D", with the month shown when it changes).
function xTicks(lo: number, hi: number): { t: number; label: string }[] {
  const span = hi - lo;
  const out: { t: number; label: string }[] = [];
  if (span > 2.5 * YR) {
    for (let y = new Date(lo).getUTCFullYear(); y <= new Date(hi).getUTCFullYear(); y++)
      out.push({ t: Date.UTC(y, 0, 1), label: String(y) });
  } else if (span > 90 * DAY) {
    const step = span > 1.1 * YR ? 3 : 1;          // months between ticks
    const d0 = new Date(lo);
    let y = d0.getUTCFullYear(), m = d0.getUTCMonth();
    if (Date.UTC(y, m, 1) < lo) m++;
    for (let cur = Date.UTC(y, m, 1); cur <= hi; ) {
      const dt = new Date(cur);
      out.push({ t: cur, label: dt.getUTCMonth() === 0 ? String(dt.getUTCFullYear()) : MONTHS[dt.getUTCMonth()] });
      cur = Date.UTC(dt.getUTCFullYear(), dt.getUTCMonth() + step, 1);
    }
  } else {
    const stepDays = Math.max(1, Math.round(span / DAY / 7));   // ~7 ticks across the view
    const d0 = new Date(lo);
    let cur = Date.UTC(d0.getUTCFullYear(), d0.getUTCMonth(), d0.getUTCDate());
    if (cur < lo) cur += DAY;
    let lastMonth = -1;
    for (; cur <= hi; cur += stepDays * DAY) {
      const dt = new Date(cur), mo = dt.getUTCMonth();
      out.push({ t: cur, label: mo !== lastMonth ? `${MONTHS[mo]} ${dt.getUTCDate()}` : String(dt.getUTCDate()) });
      lastMonth = mo;
    }
  }
  return out;
}

export default function LabChart({ series, from, to, height, onPick, onViewChange }: {
  series: LabSeries; from?: string; to?: string; height?: number;
  onPick?: (p: LabPoint | null) => void;
  onViewChange?: (from: string, to: string) => void;
}) {
  const [sel, setSel] = useState<number | null>(null);
  const pts = series.points;
  const svgRef = useRef<SVGSVGElement>(null);
  const clip = useId().replace(/:/g, "");                  // unique plot-area clip id

  // Full data extent (with a little pad for a single point) = the zoom-out limit.
  const [dataLo, dataHi] = useMemo(() => {
    const lo = series.domain ? ms(series.domain.from) : 0;
    const hi = series.domain ? ms(series.domain.to) : 1;
    return hi > lo ? [lo, hi] : [lo - 15 * DAY, lo + 15 * DAY];
  }, [series]);

  // The visible window — initialised from the from/to props (the AI/preset window), then
  // driven by pan/zoom gestures. Resets when the series or requested window changes.
  const initial = (): { lo: number; hi: number } => {
    const lo = from ? ms(from) : dataLo;
    const hi = to ? ms(to) : dataHi;
    return hi > lo ? { lo, hi } : { lo: dataLo, hi: dataHi };
  };
  const [win, setWin] = useState(initial);
  const winRef = useRef(win);                                // latest window, for reporting on gesture end
  useEffect(() => { const w = initial(); winRef.current = w; setWin(w); }, [series, from, to]);   // eslint-disable-line react-hooks/exhaustive-deps

  const pointers = useRef<Map<number, number>>(new Map());
  const prevDist = useRef<number | null>(null);
  const dragged = useRef(false);

  function clampPos(lo: number, hi: number) {
    const span = hi - lo;
    if (lo < dataLo) { lo = dataLo; hi = lo + span; }
    if (hi > dataHi) { hi = dataHi; lo = hi - span; }
    if (lo < dataLo) lo = dataLo;
    return { lo, hi };
  }
  function panBy(dxPx: number) {
    const w = svgRef.current?.getBoundingClientRect().width || 1;
    setWin((p) => {
      const span = p.hi - p.lo;
      const d = -dxPx * (VW / w) * (span / PW);
      return (winRef.current = clampPos(p.lo + d, p.hi + d));
    });
  }
  function zoomAt(midClientX: number, factor: number) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    setWin((p) => {
      const span = p.hi - p.lo;
      const frac = Math.max(0, Math.min(1, ((midClientX - rect.left) * (VW / rect.width) - M.l) / PW));
      const anchor = p.lo + frac * span;
      const maxSpan = Math.max(MIN_SPAN, dataHi - dataLo);
      const newSpan = Math.max(MIN_SPAN, Math.min(span * factor, maxSpan));
      return (winRef.current = clampPos(anchor - frac * newSpan, anchor - frac * newSpan + newSpan));
    });
  }
  // Report the window UP only on gesture end (not mid-drag), so the parent can keep it when
  // switching analytes; and so an active pan isn't fought by a prop round-trip.
  function report() { onViewChange?.(isoOf(winRef.current.lo), isoOf(winRef.current.hi)); }
  function resetView() { const w = { lo: dataLo, hi: dataHi }; winRef.current = w; setWin(w); report(); }

  function onDown(e: React.PointerEvent<SVGSVGElement>) {
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    pointers.current.set(e.pointerId, e.clientX);
    dragged.current = false;
    if (pointers.current.size >= 2 && svgRef.current) svgRef.current.style.touchAction = "none";
  }
  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    if (!pointers.current.has(e.pointerId)) return;
    const prevX = pointers.current.get(e.pointerId)!;
    pointers.current.set(e.pointerId, e.clientX);
    const xs = [...pointers.current.values()];
    if (xs.length >= 2) {
      const dist = Math.abs(xs[0] - xs[1]);
      if (prevDist.current && dist > 0) { zoomAt((xs[0] + xs[1]) / 2, prevDist.current / dist); dragged.current = true; }
      prevDist.current = dist;
      e.preventDefault();
    } else {
      const dx = e.clientX - prevX;
      if (Math.abs(dx) > 1) { panBy(dx); dragged.current = true; }
    }
  }
  function onUp(e: React.PointerEvent<SVGSVGElement>) {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) { prevDist.current = null; if (svgRef.current) svgRef.current.style.touchAction = "pan-y"; }
    if (pointers.current.size === 0 && dragged.current) report();   // persist the panned/zoomed view
  }

  const { lo, hi } = win;
  const view = useMemo(() => {
    const span = Math.max(1, hi - lo);
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
    return { span, ymin, ymax };
  }, [pts, series.segments, lo, hi]);
  const { ymin, ymax, span } = view;

  const x = (t: string) => M.l + ((ms(t) - lo) / span) * PW;
  const y = (v: number) => M.t + (1 - (v - ymin) / (ymax - ymin)) * PH;
  const clampX = (t: number) => M.l + (Math.min(hi, Math.max(lo, t)) - lo) / span * PW;
  const inView = (t: string) => ms(t) >= lo && ms(t) <= hi;
  const yticks = [0, 0.25, 0.5, 0.75, 1].map((f) => ymin + f * (ymax - ymin));

  // Line runs over ALL points (not just in-view), so a segment crosses the visible edge and
  // the line reaches the screen edges; the polylines are clipped to the plot area. Still break
  // at censored points and long gaps.
  const runs: LabPoint[][] = [];
  let run: LabPoint[] = [];
  for (const p of pts) {
    if (p.censored || p.v == null) { if (run.length) runs.push(run); run = []; continue; }
    if (run.length && ms(p.t) - ms(run[run.length - 1].t) > GAP_MS) { runs.push(run); run = []; }
    run.push(p);
  }
  if (run.length) runs.push(run);

  function pick(i: number | null) { if (dragged.current) return; setSel(i); onPick?.(i == null ? null : pts[i]); }
  const selP = sel != null ? pts[sel] : null;

  return (
    <svg ref={svgRef} viewBox={`0 0 ${VW} ${VH}`} width="100%" height={height ?? undefined}
         style={{ touchAction: "pan-y", userSelect: "none" }} role="img"
         aria-label={`${series.test_name} trend chart`}
         onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp}
         onDoubleClick={resetView} onClick={() => pick(null)}>
      <defs><clipPath id={clip}><rect x={M.l} y={M.t} width={PW} height={PH} /></clipPath></defs>
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
      {yticks.map((v, i) => (
        <g key={i}>
          <line x1={M.l} y1={y(v)} x2={M.l + PW} y2={y(v)} stroke="var(--border)" strokeWidth={1} />
          <text x={M.l - 6} y={y(v) + 3} textAnchor="end" fontSize={13} fill="var(--text-dim)">{v.toFixed(1)}</text>
        </g>
      ))}
      {xTicks(lo, hi).map((tk, i) => {
        const xx = clampX(tk.t);
        return <g key={i}>
          <line x1={xx} y1={M.t} x2={xx} y2={M.t + PH} stroke="var(--border)" strokeWidth={1} strokeDasharray="2 4" />
          <text x={xx} y={VH - 12} textAnchor="middle" fontSize={13} fill="var(--text-dim)">{tk.label}</text>
        </g>;
      })}
      <g clipPath={`url(#${clip})`}>
        {runs.map((r, i) => (
          <polyline key={i} fill="none" stroke="var(--accent)" strokeWidth={2}
                    points={r.map((p) => `${x(p.t)},${y(p.v as number)}`).join(" ")} />
        ))}
      </g>
      {pts.map((p, i) => p.censored && inView(p.t) ? (
        <text key={`c${i}`} x={x(p.t)} y={M.t + PH - 2} textAnchor="middle" fontSize={13}
              fill="var(--text-dim)" onClick={(ev) => { ev.stopPropagation(); pick(i); }}
              style={{ cursor: "pointer" }}>⌄</text>
      ) : null)}
      {pts.map((p, i) => !p.censored && p.v != null && inView(p.t) ? (
        <g key={i} onClick={(ev) => { ev.stopPropagation(); pick(i); }} style={{ cursor: "pointer" }}>
          <circle cx={x(p.t)} cy={y(p.v)} r={14} fill="transparent" />
          <Glyph x={x(p.t)} y={y(p.v)} status={p.status} sel={sel === i} />
        </g>
      ) : null)}
      {selP && selP.v != null && (() => {
        const tx = Math.min(M.l + PW - 196, Math.max(M.l, x(selP.t) - 96));
        return (
          <g transform={`translate(${tx} ${M.t + 6})`} pointerEvents="none">
            <rect width={192} height={58} rx={8} fill="var(--bg-elev-2)" stroke="var(--border)" />
            <text x={10} y={20} fontSize={14} fill="var(--text)" fontWeight={600}>
              {selP.vtext}{selP.unit ? " " + selP.unit : ""} · {selP.status}
            </text>
            <text x={10} y={38} fontSize={12} fill="var(--text-dim)">{selP.t}{selP.time ? " " + selP.time : ""}
              {selP.ref_text ? `  (ref ${selP.ref_text})` : ""}</text>
            <text x={10} y={52} fontSize={12} fill="var(--accent)">{selP.note_title || ""}
              {selP.source === "lab_image" ? "  · from a photo — verify" : ""}</text>
          </g>
        );
      })()}
    </svg>
  );
}
