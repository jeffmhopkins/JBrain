import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet.heat";
import { getLocations, getLocatedNotes, getPlaces, addPlace, renamePlace, deletePlace, ensurePlaceNote, LocPoint, LocatedNote, Place } from "../api";

type Mode = "trail" | "heat";
const RANGES = [
  { k: "7d", label: "7 days", days: 7 },
  { k: "30d", label: "30 days", days: 30 },
  { k: "all", label: "All", days: 0 },
];
const NOTES_HERE_M = 200;   // tap-the-map radius for "what notes are here"

const parseTs = (s: string) => new Date(s.replace(" ", "T") + "Z").getTime();
const fmt = (ms: number) =>
  new Date(ms).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const haversineM = (la1: number, lo1: number, la2: number, lo2: number) => {
  const R = 6371000, r = Math.PI / 180;
  const dLa = (la2 - la1) * r, dLo = (lo2 - lo1) * r;
  const a = Math.sin(dLa / 2) ** 2 + Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin(dLo / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
};
const noteIcon = L.divIcon({ className: "note-pin", html: "📍", iconSize: [22, 22], iconAnchor: [11, 22], popupAnchor: [0, -20] });

// Location trail over a server-proxied map (browser only talks to /api/tiles, so the
// trail's coordinates never leak to a third-party tile host). Trail / dwell-heatmap
// toggle, date-range presets, a scrub-and-play timeline, note pins, and saved places.
export default function MapPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const focus = params.get("focus");   // a note slug to center + open
  const focusPlace = params.get("place");   // a place id to center on

  const mapEl = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const overlay = useRef<L.Layer | null>(null);
  const head = useRef<L.CircleMarker | null>(null);
  const placeLayer = useRef<L.LayerGroup | null>(null);
  const noteLayer = useRef<L.LayerGroup | null>(null);
  const noteMarkers = useRef<Record<string, L.Marker>>({});
  const shownSlugs = useRef<Set<string>>(new Set());
  const notesRef = useRef<LocatedNote[]>([]);
  const addingRef = useRef(false);

  const [mode, setMode] = useState<Mode>("trail");
  const [heatLevel, setHeatLevel] = useState(0.6);   // 0 = subtle … 1 = intense
  const [rangeDays, setRangeDays] = useState(7);
  const [points, setPoints] = useState<LocPoint[]>([]);
  const [notes, setNotes] = useState<LocatedNote[]>([]);
  const [idx, setIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const [places, setPlaces] = useState<Place[]>([]);
  const [showPlaces, setShowPlaces] = useState(false);
  const [showNotes, setShowNotes] = useState(true);
  const [adding, setAdding] = useState(false);

  const loadPlaces = () => getPlaces().then(setPlaces).catch(() => setPlaces([]));
  const openPlaceNotes = (id: number) => ensurePlaceNote(id).then((r) => navigate(`/note/${r.slug}`)).catch(() => {});
  const savePlaceAt = (lat: number, lon: number, suggested: string) => {
    const name = window.prompt("Place name:", suggested)?.trim();
    if (name) addPlace({ name, lat, lon, radius_m: 150 }).then(loadPlaces).catch(() => {});
  };

  useEffect(() => { loadPlaces(); }, []);
  useEffect(() => { addingRef.current = adding; }, [adding]);
  useEffect(() => { notesRef.current = notes; }, [notes]);

  // A popup for a note pin: open it, or save its spot as a place. Built as a DOM
  // node so the link can use client-side routing instead of a full reload.
  const notePopup = (n: LocatedNote) => {
    const div = document.createElement("div");
    div.className = "note-pop";
    const a = document.createElement("a");
    a.textContent = n.title; a.href = `/note/${n.slug}`;
    a.onclick = (e) => { e.preventDefault(); navigate(`/note/${n.slug}`); };
    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = n.created_at.slice(0, 10) + (n.location_label ? ` · ${n.location_label}` : "");
    const save = document.createElement("button");
    save.textContent = "Save as place";
    save.onclick = () => savePlaceAt(n.lat, n.lon, n.title.split("/").pop() || n.title);
    div.append(a, meta, save);
    return div;
  };

  useEffect(() => {
    if (!mapEl.current || map.current) return;
    const m = L.map(mapEl.current).setView([20, 0], 2);
    L.tileLayer("/api/tiles/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(m);
    m.on("click", (e: L.LeafletMouseEvent) => {
      // In "add place" mode a tap drops a place; otherwise a tap on empty map shows
      // the notes captured near that spot ("notes here").
      if (addingRef.current) {
        setAdding(false);
        savePlaceAt(e.latlng.lat, e.latlng.lng, "");
        return;
      }
      const here = notesRef.current
        .map((n) => [haversineM(e.latlng.lat, e.latlng.lng, n.lat, n.lon), n] as [number, LocatedNote])
        .filter(([d]) => d <= NOTES_HERE_M)
        .sort((a, b) => a[0] - b[0]);
      if (!here.length) return;
      const div = document.createElement("div");
      div.className = "note-pop";
      const h = document.createElement("strong");
      h.textContent = `${here.length} note${here.length > 1 ? "s" : ""} here`;
      div.append(h);
      here.slice(0, 12).forEach(([, n]) => {
        const a = document.createElement("a");
        a.textContent = n.title; a.href = `/note/${n.slug}`;
        a.onclick = (ev) => { ev.preventDefault(); navigate(`/note/${n.slug}`); };
        div.append(a);
      });
      L.popup().setLatLng(e.latlng).setContent(div).openOn(m);
    });
    map.current = m;
    setTimeout(() => m.invalidateSize(), 200);
    return () => { m.remove(); map.current = null; };
  }, []);

  // Load both the trail and the located notes for the chosen range.
  useEffect(() => {
    setLoading(true);
    const since = rangeDays > 0 ? new Date(Date.now() - rangeDays * 86400000).toISOString() : undefined;
    Promise.all([getLocations(since).catch(() => []), getLocatedNotes(since).catch(() => [])])
      .then(([pts, ns]) => { setPoints(pts); setNotes(ns); setPlaying(false); })
      .finally(() => setLoading(false));
  }, [rangeDays]);

  // The scrubber walks the UNION of fix-times and note-times, so notes still appear
  // (and play back) even on days the background trail wasn't running.
  const timeline = useMemo(() => {
    const ts = new Set<number>();
    points.forEach((p) => ts.add(parseTs(p.recorded_at)));
    notes.forEach((n) => ts.add(parseTs(n.created_at)));
    return [...ts].sort((a, b) => a - b);
  }, [points, notes]);
  useEffect(() => { setIdx(Math.max(0, timeline.length - 1)); }, [timeline.length]);

  const curTs = timeline[idx] ?? 0;

  // Fit to everything we have (trail + notes) on load.
  useEffect(() => {
    const m = map.current; if (!m) return;
    const coords = [
      ...points.map((p) => [p.lat, p.lon] as [number, number]),
      ...notes.map((n) => [n.lat, n.lon] as [number, number]),
    ];
    if (coords.length) m.fitBounds(L.latLngBounds(coords), { padding: [30, 30], maxZoom: 16 });
  }, [points, notes]);

  // ?focus=<slug>: center on that note and open its pin, regardless of scrub.
  useEffect(() => {
    if (!focus || !notes.length) return;
    const n = notes.find((x) => x.slug === focus);
    if (!n) return;
    map.current?.setView([n.lat, n.lon], 16);
    const t = window.setTimeout(() => noteMarkers.current[focus]?.openPopup(), 350);
    return () => clearTimeout(t);
  }, [focus, notes]);

  // ?place=<id>: center on a saved place (deep-linked from its loc/ note page).
  useEffect(() => {
    if (!focusPlace || !places.length) return;
    const p = places.find((x) => String(x.id) === focusPlace);
    if (p) map.current?.setView([p.lat, p.lon], 16);
  }, [focusPlace, places]);

  // Draw each saved geofence as a labeled circle so the radius is visible.
  useEffect(() => {
    const m = map.current; if (!m) return;
    if (placeLayer.current) { m.removeLayer(placeLayer.current); placeLayer.current = null; }
    if (!places.length) return;
    placeLayer.current = L.layerGroup(
      places.map((p) =>
        L.circle([p.lat, p.lon], { radius: p.radius_m, color: "#ffb300", weight: 1.5, fillOpacity: 0.08 })
          .bindTooltip(p.name, { permanent: true, direction: "center", className: "place-label" })
          // Tap a geofence → its loc/ note, except while dropping a new place.
          .on("click", () => { if (!addingRef.current) openPlaceNotes(p.id); })),
    ).addTo(m);
  }, [places]);

  // Build the note markers ONCE per note-set change (not per scrub tick) and keep an
  // empty layer group on the map; scrubbing only reconciles which markers are in it.
  useEffect(() => {
    const m = map.current; if (!m) return;
    if (noteLayer.current) { m.removeLayer(noteLayer.current); }
    noteMarkers.current = {};
    shownSlugs.current = new Set();
    const g = L.layerGroup();
    if (showNotes) {
      notes.forEach((n) => {
        noteMarkers.current[n.slug] = L.marker([n.lat, n.lon], { icon: noteIcon }).bindPopup(() => notePopup(n));
      });
    }
    g.addTo(m);
    noteLayer.current = g;
  }, [notes, showNotes]);

  // Reconcile membership against the scrub time — add/remove only the delta, so a
  // marker (and any open popup) is never needlessly recreated during playback.
  useEffect(() => {
    const g = noteLayer.current; if (!g) return;
    for (const n of notes) {
      const want = parseTs(n.created_at) <= curTs;
      const mk = noteMarkers.current[n.slug];
      if (!mk) continue;
      const has = shownSlugs.current.has(n.slug);
      if (want && !has) { g.addLayer(mk); shownSlugs.current.add(n.slug); }
      else if (!want && has) { g.removeLayer(mk); shownSlugs.current.delete(n.slug); }
    }
  }, [notes, curTs, showNotes]);

  // Dwell weight: time gap to the NEXT fix (capped) → places you lingered glow hotter.
  const heat = useMemo(
    () => points.map((p, i) => {
      const next = points[i + 1];
      const gapMin = next ? Math.min(120, (parseTs(next.recorded_at) - parseTs(p.recorded_at)) / 60000) : 30;
      return [p.lat, p.lon, Math.max(0.15, gapMin / 120)] as [number, number, number];
    }),
    [points],
  );

  // Trail / heat overlay up to the current scrub time.
  useEffect(() => {
    const m = map.current; if (!m) return;
    if (overlay.current) { m.removeLayer(overlay.current); overlay.current = null; }
    if (head.current) { m.removeLayer(head.current); head.current = null; }
    const upto = points.filter((p) => parseTs(p.recorded_at) <= curTs);
    if (!upto.length) return;
    if (mode === "trail") {
      overlay.current = L.polyline(upto.map((p) => [p.lat, p.lon] as [number, number]),
        { color: "#4ea1ff", weight: 3, opacity: 0.85 }).addTo(m);
      const last = upto[upto.length - 1];
      head.current = L.circleMarker([last.lat, last.lon],
        { radius: 6, color: "#fff", weight: 2, fillColor: "#4ea1ff", fillOpacity: 1 }).addTo(m);
    } else if ((L as any).heatLayer) {
      const hUpto = heat.filter((_, i) => parseTs(points[i].recorded_at) <= curTs);
      // The slider drives intensity: a higher level lowers `max` (more of the trail
      // reaches "hot") and grows the radius (bigger shading), and vice-versa.
      const heatMax = Math.max(0.12, 1 - heatLevel * 0.85);   // higher level → hotter
      const radius = Math.round(26 + heatLevel * 26);          // ~26–52 px
      overlay.current = (L as any).heatLayer(hUpto, {
        radius, blur: Math.round(radius * 0.7), max: heatMax, minOpacity: 0.3, maxZoom: 17,
      }).addTo(m);
    } else {
      overlay.current = L.layerGroup(
        upto.map((p, i) => L.circleMarker([p.lat, p.lon],
          { radius: 4 + 10 * heat[i][2], stroke: false, fillColor: "#ff7043", fillOpacity: 0.35 })),
      ).addTo(m);
    }
  }, [points, mode, curTs, heat, heatLevel]);

  useEffect(() => {
    if (!playing || timeline.length < 2) return;
    if (idx >= timeline.length - 1) { setPlaying(false); return; }
    const id = window.setTimeout(() => setIdx((i) => Math.min(timeline.length - 1, i + 1)), 120);
    return () => clearTimeout(id);
  }, [playing, idx, timeline.length]);

  return (
    <div className="map-tool">
      <div className="map-bar">
        <div className="seg">
          <button className={mode === "trail" ? "on" : ""} onClick={() => setMode("trail")}>Trail</button>
          <button className={mode === "heat" ? "on" : ""} onClick={() => setMode("heat")}>Heatmap</button>
        </div>
        {mode === "heat" && (
          <label className="heat-slider" title="Heat intensity">
            <span className="muted">less</span>
            <input type="range" min={0} max={100} value={Math.round(heatLevel * 100)}
                   onChange={(e) => setHeatLevel(+e.target.value / 100)} />
            <span className="muted">more</span>
          </label>
        )}
        <span className="spacer" />
        <div className="seg">
          {RANGES.map((r) => (
            <button key={r.k} className={rangeDays === r.days ? "on" : ""} onClick={() => setRangeDays(r.days)}>{r.label}</button>
          ))}
        </div>
        <div className="seg">
          <button className={showNotes ? "on" : ""} onClick={() => setShowNotes((s) => !s)}>Notes</button>
          <button className={showPlaces ? "on" : ""} onClick={() => setShowPlaces((s) => !s)}>Places</button>
        </div>
      </div>
      <div ref={mapEl} className="map-canvas" />
      {showPlaces && (
        <div className="places-panel">
          <div className="places-head">
            <strong>Places</strong>
            <button className={adding ? "on" : ""} onClick={() => setAdding((a) => !a)}>
              {adding ? "Tap the map…" : "+ Add"}
            </button>
          </div>
          {places.length === 0 ? (
            <p className="places-empty">No places yet. Tap “+ Add”, then tap the map to drop one.</p>
          ) : (
            <ul className="places-list">
              {places.map((p) => (
                <li key={p.id}>
                  <button className="place-go" onClick={() => map.current?.setView([p.lat, p.lon], 16)}>{p.name}</button>
                  <span className="place-r">{p.radius_m} m</span>
                  <button className="place-del" title="Open notes" onClick={() => openPlaceNotes(p.id)}>📝</button>
                  <button className="place-del" title="Rename" onClick={() => {
                    const name = window.prompt("Rename place:", p.name)?.trim();
                    if (name && name !== p.name) renamePlace(p.id, name).then(loadPlaces);
                  }}>✎</button>
                  <button className="place-del" title="Delete" onClick={() => deletePlace(p.id).then(loadPlaces)}>✕</button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <div className="map-play">
        <button className="icon-btn" disabled={timeline.length < 2}
                onClick={() => { if (idx >= timeline.length - 1) setIdx(0); setPlaying((p) => !p); }}>
          {playing ? "❚❚" : "▶"}
        </button>
        <input type="range" min={0} max={Math.max(0, timeline.length - 1)} value={idx}
               disabled={timeline.length < 2}
               onChange={(e) => { setPlaying(false); setIdx(+e.target.value); }} />
        <span className="map-time">
          {loading ? "Loading…" : timeline.length ? `${fmt(curTs)}  ·  ${idx + 1}/${timeline.length}` : "No location data yet"}
        </span>
      </div>
    </div>
  );
}
