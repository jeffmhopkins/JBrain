import { useEffect, useState } from "react";
import { LabSeries, getShareLabSeries, getResearchLabSeries } from "../api";
import LabChart from "./LabChart";

// One scoped chart for a SHARE recipient: fetches its series for THIS token through the
// token-scoped, allow-list-rechecked, no-store endpoint and renders the shared LabChart SVG.
// `variant` picks the endpoint: 'labs' (data-only labs share) or 'research' (assisted share,
// where the AI surfaces charts on demand). A failed/empty chart degrades quietly.
export default function ShareChart({ token, analyte, from, to, title, variant = "labs" }: {
  token: string; analyte: string; from?: string; to?: string; title?: string;
  variant?: "labs" | "research";
}) {
  const [series, setSeries] = useState<LabSeries | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    setSeries(null); setErr(false);
    const fetchSeries = variant === "research" ? getResearchLabSeries : getShareLabSeries;
    fetchSeries(token, analyte).then(setSeries).catch(() => setErr(true));
  }, [token, analyte, variant]);
  if (err) return (
    <div className="lab-chart-wrap"><div className="lab-head"><strong>{title || analyte}</strong></div>
      <p className="muted" style={{ fontSize: 12, margin: 0 }}>Couldn’t load this result.</p></div>
  );
  if (!series) return <div className="muted" style={{ fontSize: 12, padding: 8 }}>Loading {title || analyte}…</div>;
  if (!series.points.length) return null;
  return (
    <div className="lab-chart-wrap">
      <div className="lab-head"><strong>{series.test_name}</strong>
        {series.unit && <span className="muted" style={{ fontSize: 12 }}> ({series.unit})</span>}</div>
      <LabChart series={series} from={from} to={to} height={170} />
    </div>
  );
}
