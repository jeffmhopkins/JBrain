import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { LabSeries, getLabSeries } from "../api";
import LabChart from "./LabChart";

// A lab chart rendered inline in chat. The chat message stores only a thin SPEC
// ({analyte, unit, window}); this card re-fetches the LIVE series — so no lab values are
// copied into the conversation and a later-corrected result self-heals on reload.
export interface ChartSpec { analyte: string; unit?: string | null; from?: string | null; to?: string | null; title?: string; }

export default function LabChartCard({ spec }: { spec: ChartSpec }) {
  const [s, setS] = useState<LabSeries | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    let live = true;
    getLabSeries(spec.analyte, spec.unit || undefined).then((r) => live && setS(r)).catch(() => live && setErr(true));
    return () => { live = false; };
  }, [spec.analyte, spec.unit]);

  const title = spec.title || spec.analyte;
  if (err) return <div className="lab-card muted">Couldn’t load {title}.</div>;
  if (!s) return <div className="lab-card muted">Charting {title}…</div>;
  if (!s.points.length) return <div className="lab-card muted">No results on file for {s.test_name}.</div>;
  return (
    <div className="lab-card">
      <div className="lab-card-head">
        <strong>{s.test_name}</strong>{s.unit && <span className="muted"> ({s.unit})</span>}
        <Link to="/labs" className="lab-src">Open Labs →</Link>
      </div>
      <LabChart series={s} from={spec.from || undefined} to={spec.to || undefined} />
      <div className="muted lab-card-foot">Your own results — not a diagnostic tool; verify against your reports.</div>
    </div>
  );
}
