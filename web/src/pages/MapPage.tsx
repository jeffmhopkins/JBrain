import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet.heat";
import { getLocations, LocPoint } from "../api";

type Mode = "trail" | "heat";
const RANGES = [
  { k: "7d", label: "7 days", days: 7 },
  { k: "30d", label: "30 days", days: 30 },
  { k: "all", label: "All", days: 0 },
];

const parseTs = (s: string) => new Date(s.replace(" ", "T") + "Z").getTime();
const fmt = (ms: number) =>
  new Date(ms).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

// Location trail over a server-proxied map (browser only talks to /api/tiles, so the
// trail's coordinates never leak to a third-party tile host). Trail / dwell-heatmap
// toggle, date-range presets, and a scrub-and-play timeline.
export default function MapPage() {
  const mapEl = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const overlay = useRef<L.Layer | null>(null);
  const head = useRef<L.CircleMarker | null>(null);

  const [mode, setMode] = useState<Mode>("trail");
  const [rangeDays, setRangeDays] = useState(7);
  const [points, setPoints] = useState<LocPoint[]>([]);
  const [idx, setIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!mapEl.current || map.current) return;
    const m = L.map(mapEl.current).setView([20, 0], 2);
    L.tileLayer("/api/tiles/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(m);
    map.current = m;
    setTimeout(() => m.invalidateSize(), 200);
    return () => { m.remove(); map.current = null; };
  }, []);

  useEffect(() => {
    setLoading(true);
    const since = rangeDays > 0 ? new Date(Date.now() - rangeDays * 86400000).toISOString() : undefined;
    getLocations(since)
      .then((pts) => {
        setPoints(pts);
        setIdx(pts.length ? pts.length - 1 : 0);
        setPlaying(false);
        if (map.current && pts.length) {
          map.current.fitBounds(L.latLngBounds(pts.map((p) => [p.lat, p.lon] as [number, number])),
            { padding: [30, 30], maxZoom: 16 });
        }
      })
      .catch(() => setPoints([]))
      .finally(() => setLoading(false));
  }, [rangeDays]);

  // Dwell weight: time gap to the NEXT fix (capped) → places you lingered glow hotter.
  const heat = useMemo(
    () => points.map((p, i) => {
      const next = points[i + 1];
      const gapMin = next ? Math.min(120, (parseTs(next.recorded_at) - parseTs(p.recorded_at)) / 60000) : 30;
      return [p.lat, p.lon, Math.max(0.15, gapMin / 120)] as [number, number, number];
    }),
    [points],
  );

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    if (overlay.current) { m.removeLayer(overlay.current); overlay.current = null; }
    if (head.current) { m.removeLayer(head.current); head.current = null; }
    if (!points.length) return;
    const upto = points.slice(0, idx + 1);
    if (mode === "trail") {
      const line = L.polyline(upto.map((p) => [p.lat, p.lon] as [number, number]),
        { color: "#4ea1ff", weight: 3, opacity: 0.85 }).addTo(m);
      overlay.current = line;
      const last = upto[upto.length - 1];
      if (last) {
        head.current = L.circleMarker([last.lat, last.lon],
          { radius: 6, color: "#fff", weight: 2, fillColor: "#4ea1ff", fillOpacity: 1 }).addTo(m);
      }
    } else if ((L as any).heatLayer) {
      overlay.current = (L as any).heatLayer(heat.slice(0, idx + 1), { radius: 22, blur: 18, maxZoom: 17 }).addTo(m);
    } else {
      // Fallback if the heat plugin didn't load: translucent dwell-sized dots.
      const g = L.layerGroup(
        upto.map((p, i) => L.circleMarker([p.lat, p.lon],
          { radius: 4 + 10 * heat[i][2], stroke: false, fillColor: "#ff7043", fillOpacity: 0.35 })),
      ).addTo(m);
      overlay.current = g;
    }
  }, [points, mode, idx, heat]);

  useEffect(() => {
    if (!playing || points.length < 2) return;
    if (idx >= points.length - 1) { setPlaying(false); return; }
    const id = window.setTimeout(() => setIdx((i) => Math.min(points.length - 1, i + 1)), 120);
    return () => clearTimeout(id);
  }, [playing, idx, points.length]);

  const curTs = points[idx] ? parseTs(points[idx].recorded_at) : 0;

  return (
    <div className="map-tool">
      <div className="map-bar">
        <div className="seg">
          <button className={mode === "trail" ? "on" : ""} onClick={() => setMode("trail")}>Trail</button>
          <button className={mode === "heat" ? "on" : ""} onClick={() => setMode("heat")}>Heatmap</button>
        </div>
        <span className="spacer" />
        <div className="seg">
          {RANGES.map((r) => (
            <button key={r.k} className={rangeDays === r.days ? "on" : ""} onClick={() => setRangeDays(r.days)}>{r.label}</button>
          ))}
        </div>
      </div>
      <div ref={mapEl} className="map-canvas" />
      <div className="map-play">
        <button className="icon-btn" disabled={points.length < 2}
                onClick={() => { if (idx >= points.length - 1) setIdx(0); setPlaying((p) => !p); }}>
          {playing ? "❚❚" : "▶"}
        </button>
        <input type="range" min={0} max={Math.max(0, points.length - 1)} value={idx}
               disabled={points.length < 2}
               onChange={(e) => { setPlaying(false); setIdx(+e.target.value); }} />
        <span className="map-time">
          {loading ? "Loading…" : points.length ? `${fmt(curTs)}  ·  ${idx + 1}/${points.length}` : "No location points yet"}
        </span>
      </div>
    </div>
  );
}
