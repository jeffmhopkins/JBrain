import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet.heat";
import { getLocations, getLocatedNotes, getPlaces, addPlace, updatePlace, deletePlace, ensurePlaceNote, getPeople, LocPoint, LocatedNote, Place, Person } from "../api";

type Mode = "trail" | "heat";
const RANGES = [
  { k: "7d", label: "7 days", days: 7 },
  { k: "30d", label: "30 days", days: 30 },
  { k: "all", label: "All", days: 0 },
];
const NOTES_HERE_M = 200;   // tap-the-map radius for "what notes are here"
const LIVE_POLL_MS = 15000; // how often to pull newly-arrived fixes onto the live map

const parseTs = (s: string) => new Date(s.replace(" ", "T") + "Z").getTime();
const fmt = (ms: number) =>
  new Date(ms).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const haversineM = (la1: number, lo1: number, la2: number, lo2: number) => {
  const R = 6371000, r = Math.PI / 180;
  const dLa = (la2 - la1) * r, dLo = (lo2 - lo1) * r;
  const a = Math.sin(dLa / 2) ** 2 + Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin(dLo / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
};

// Perpendicular distance (in lat/lon degree-space) from p to segment a–b, with the
// projection clamped to the segment. Degree-space is fine: the DP tolerance below is
// also in degrees, so the comparison is internally consistent and cheap. Zero-length
// segments (a long dwell at one spot) fall back to the point distance — never NaN.
const perpDist = (p: [number, number], a: [number, number], b: [number, number]) => {
  const dy = b[0] - a[0], dx = b[1] - a[1];
  const len2 = dy * dy + dx * dx;
  let t = len2 ? ((p[0] - a[0]) * dy + (p[1] - a[1]) * dx) / len2 : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  const ey = p[0] - (a[0] + t * dy), ex = p[1] - (a[1] + t * dx);
  return Math.sqrt(ey * ey + ex * ex);
};
// Iterative Douglas–Peucker (explicit stack, so 12k-point trails can't blow the call
// stack). ALWAYS preserves the first and last vertex, so a simplified trail still ends
// exactly on the person's newest fix — the head dot never detaches from the line.
const simplifyDP = (pts: [number, number][], eps: number): [number, number][] => {
  const n = pts.length;
  if (n <= 2 || eps <= 0) return pts;
  const keep = new Uint8Array(n);
  keep[0] = keep[n - 1] = 1;
  const stack: [number, number][] = [[0, n - 1]];
  while (stack.length) {
    const [lo, hi] = stack.pop()!;
    let maxD = 0, idx = -1;
    for (let i = lo + 1; i < hi; i++) {
      const d = perpDist(pts[i], pts[lo], pts[hi]);
      if (d > maxD) { maxD = d; idx = i; }
    }
    if (idx !== -1 && maxD > eps) { keep[idx] = 1; stack.push([lo, idx], [idx, hi]); }
  }
  const out: [number, number][] = [];
  for (let i = 0; i < n; i++) if (keep[i]) out.push(pts[i]);
  return out;
};
// Map the settled zoom to a DP tolerance in degrees (~1.5 px of detail at that zoom).
// Zoomed out we collapse aggressively; at z≥17 we disable simplification so a close-up
// inspection sees the raw track. Re-runs whenever the zoom level changes.
const epsilonForZoom = (zoom: number) => (zoom >= 17 ? 0 : (360 / 256 / 2 ** zoom) * 1.5);

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
  const head = useRef<L.Layer | null>(null);
  const placeLayer = useRef<L.LayerGroup | null>(null);
  const noteLayer = useRef<L.LayerGroup | null>(null);
  const noteMarkers = useRef<Record<string, L.Marker>>({});
  const shownSlugs = useRef<Set<string>>(new Set());
  const notesRef = useRef<LocatedNote[]>([]);
  const addingRef = useRef(false);
  const pointsRef = useRef<LocPoint[]>([]);     // latest points, for the live poller
  const followingRef = useRef(true);            // scrubber pinned to the live edge?
  const needFitRef = useRef(true);              // refit bounds only on (re)load, not live appends

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
  // Inline place editor (name + geofence radius); the radius previews live on the map.
  const [editing, setEditing] = useState<{ id: number; name: string; radius: number } | null>(null);
  // People: colour the trail by who each fix belongs to (matched from its source).
  const [people, setPeople] = useState<Person[]>([]);
  const [hidden, setHidden] = useState<Set<number>>(new Set());   // person ids toggled off
  const [zoomLevel, setZoomLevel] = useState(2);   // drives the trail's DP tolerance; updated on zoomend

  const loadPlaces = () => getPlaces().then(setPlaces).catch(() => setPlaces([]));

  // Commit the inline place edit (name and/or geofence radius); only send changes.
  async function saveEditing() {
    if (!editing) return;
    const p = places.find((x) => x.id === editing.id);
    const name = editing.name.trim();
    const radius = Math.round(editing.radius);
    const body: { name?: string; radius_m?: number } = {};
    if (name && (!p || name !== p.name)) body.name = name;
    if (!p || radius !== p.radius_m) body.radius_m = radius;
    setEditing(null);
    if (Object.keys(body).length) {
      try { await updatePlace(editing.id, body); await loadPlaces(); }
      catch (e: any) { alert(e?.message || "Couldn't update place."); }
    }
  }
  const openPlaceNotes = (id: number) => ensurePlaceNote(id).then((r) => navigate(`/note/${r.slug}`)).catch(() => {});
  const savePlaceAt = (lat: number, lon: number, suggested: string) => {
    const name = window.prompt("Place name:", suggested)?.trim();
    if (name) addPlace({ name, lat, lon, radius_m: 150 }).then(loadPlaces).catch(() => {});
  };

  useEffect(() => { loadPlaces(); }, []);
  useEffect(() => { getPeople().then(setPeople).catch(() => {}); }, []);

  // Resolve a fix's `source` to a person (by name/alias, else default) → its colour.
  const personOf = useMemo(() => {
    const byKey = new Map<string, Person>();
    for (const p of people) {
      byKey.set(p.name.toLowerCase(), p);
      for (const a of p.aliases.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean)) byKey.set(a, p);
    }
    const def = people.find((p) => p.is_default) || people[0];
    return (source?: string | null) => byKey.get((source || "").trim().toLowerCase()) || def;
  }, [people]);
  // People who actually appear in the loaded trail (drives the filter chips).
  const presentPeople = useMemo(() => {
    const ids = new Set<number>();
    for (const p of points) { const per = personOf(p.source); if (per) ids.add(per.id); }
    return people.filter((p) => ids.has(p.id));
  }, [points, people, personOf]);
  useEffect(() => { addingRef.current = adding; }, [adding]);
  useEffect(() => { notesRef.current = notes; }, [notes]);
  useEffect(() => { pointsRef.current = points; }, [points]);

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
    // preferCanvas: render the trail polyline, head dots and geofence circles onto one
    // shared <canvas> instead of a DOM node per shape — the big win for panning a long
    // trail. (divIcon note pins stay DOM; leaflet.heat keeps its own canvas.)
    const m = L.map(mapEl.current, { preferCanvas: true }).setView([20, 0], 2);
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
    // Re-simplify the trail when the zoom settles (zoomend fires once per gesture, never
    // on pan). Round + functional no-op so an unchanged level doesn't churn the overlay.
    m.on("zoomend", () => { const z = Math.round(m.getZoom()); setZoomLevel((cur) => (cur === z ? cur : z)); });
    map.current = m;
    setTimeout(() => m.invalidateSize(), 200);
    return () => { m.remove(); map.current = null; };
  }, []);

  // Load both the trail and the located notes for the chosen range.
  useEffect(() => {
    setLoading(true);
    needFitRef.current = true;       // a fresh range → refit once
    followingRef.current = true;     // …and re-pin to the live edge
    const since = rangeDays > 0 ? new Date(Date.now() - rangeDays * 86400000).toISOString() : undefined;
    Promise.all([getLocations(since).catch(() => []), getLocatedNotes(since).catch(() => [])])
      .then(([pts, ns]) => { setPoints(pts); setNotes(ns); setPlaying(false); })
      .finally(() => setLoading(false));
  }, [rangeDays]);

  // LIVE: poll for fixes newer than the last one we hold and append them (deduped by
  // id). The trail redraws and the head dot moves on its own as packets land.
  useEffect(() => {
    const tick = async () => {
      if (typeof document !== "undefined" && document.hidden) return;   // pause when tab/app is backgrounded
      const cur = pointsRef.current;
      const lastTs = cur.length ? cur[cur.length - 1].recorded_at : null;
      const since = lastTs
        ? new Date(lastTs.replace(" ", "T") + "Z").toISOString()
        : (rangeDays > 0 ? new Date(Date.now() - rangeDays * 86400000).toISOString() : undefined);
      try {
        const fresh = await getLocations(since);
        if (!fresh?.length) return;
        const seen = new Set(cur.map((p) => p.id));
        const add = fresh.filter((p) => !seen.has(p.id));
        if (add.length) setPoints((prev) => [...prev, ...add]);
      } catch { /* transient — try again next tick */ }
    };
    const id = window.setInterval(tick, LIVE_POLL_MS);
    return () => window.clearInterval(id);
  }, [rangeDays]);

  // The scrubber walks the UNION of fix-times and note-times, so notes still appear
  // (and play back) even on days the background trail wasn't running.
  const timeline = useMemo(() => {
    const ts = new Set<number>();
    points.forEach((p) => ts.add(parseTs(p.recorded_at)));
    notes.forEach((n) => ts.add(parseTs(n.created_at)));
    return [...ts].sort((a, b) => a - b);
  }, [points, notes]);
  // Follow the live edge only while the user is pinned there; if they've scrubbed back
  // (or are playing history), new fixes append silently without yanking the scrubber.
  useEffect(() => {
    if (followingRef.current) setIdx(Math.max(0, timeline.length - 1));
  }, [timeline.length]);

  const curTs = timeline[idx] ?? 0;

  // Fit to everything we have (trail + notes) ONCE per (re)load — never on a live
  // append, so polling doesn't keep snapping the user's pan/zoom back.
  useEffect(() => {
    const m = map.current; if (!m || !needFitRef.current) return;
    const coords = [
      ...points.map((p) => [p.lat, p.lon] as [number, number]),
      ...notes.map((n) => [n.lat, n.lon] as [number, number]),
    ];
    if (coords.length) { m.fitBounds(L.latLngBounds(coords), { padding: [30, 30], maxZoom: 16 }); needFitRef.current = false; }
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
      places.map((p) => {
        // While this place is being edited, draw it at the in-progress radius (live
        // preview) and brighten its ring so the one you're resizing stands out.
        const live = editing && editing.id === p.id;
        const radius = live ? editing.radius : p.radius_m;
        return L.circle([p.lat, p.lon],
          { radius, color: live ? "#ffd54f" : "#ffb300", weight: live ? 3 : 2, fillOpacity: live ? 0.18 : 0.1 })
          .bindTooltip(live ? `${p.name} · ${radius} m` : p.name,
            { permanent: true, direction: "top", className: "place-label", offset: [0, -4] })
          // Tap a geofence → its loc/ note, except while dropping a new place.
          .on("click", () => { if (!addingRef.current) openPlaceNotes(p.id); });
      }),
    ).addTo(m);
  }, [places, editing]);

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
    // A point is shown unless its person is toggled off.
    const visible = (p: LocPoint) => { const per = personOf(p.source); return !per || !hidden.has(per.id); };
    const upto = points.filter((p) => parseTs(p.recorded_at) <= curTs && visible(p));
    if (!upto.length) return;
    if (mode === "trail") {
      // One polyline per person in their colour (different people aren't joined); track
      // each person's MOST RECENT fix (upto is chronological) for a labeled head dot.
      const byPerson = new Map<number, { color: string; name: string; pts: [number, number][]; last: LocPoint }>();
      for (const p of upto) {
        const per = personOf(p.source);
        const key = per?.id ?? -1;
        const g = byPerson.get(key) ?? { color: per?.color || "#4ea1ff", name: per?.name || "", pts: [], last: p };
        g.pts.push([p.lat, p.lon]);
        g.last = p;   // chronological order → ends on this person's newest fix
        byPerson.set(key, g);
      }
      // Thin each person's track to ~1.5 px of detail for the current zoom before drawing.
      // DP keeps the endpoints, so the line still ends on `last` (the head dot). Heat mode
      // is left untouched below — it needs every fix for its dwell weighting. Read the live
      // zoom (zoomLevel only triggers this rebuild) so the first paint after fitBounds is
      // already at the right detail instead of flashing the zoom-2 (over-collapsed) track.
      const eps = epsilonForZoom(m.getZoom());
      const group = L.layerGroup();
      for (const { color, pts } of byPerson.values()) {
        L.polyline(eps > 0 ? simplifyDP(pts, eps) : pts, { color, weight: 3, opacity: 0.85 }).addTo(group);
      }
      overlay.current = group.addTo(m);
      // Each person's latest position as a white-ringed dot, labeled with their name.
      const heads = L.layerGroup();
      for (const { color, name, last } of byPerson.values()) {
        const dot = L.circleMarker([last.lat, last.lon],
          { radius: 6, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1 });
        if (name) dot.bindTooltip(name, { permanent: true, direction: "top", className: "head-label", offset: [0, -6] });
        dot.addTo(heads);
      }
      head.current = heads.addTo(m);
    } else if ((L as any).heatLayer) {
      const hUpto = heat.filter((_, i) => parseTs(points[i].recorded_at) <= curTs && visible(points[i]));
      // The slider drives intensity: a higher level lowers `max` (more of the trail
      // reaches "hot") and grows the radius (bigger shading), and vice-versa.
      const heatMax = Math.max(0.12, 1 - heatLevel * 0.85);   // higher level → hotter
      const radius = Math.round(26 + heatLevel * 26);          // ~26–52 px
      overlay.current = (L as any).heatLayer(hUpto, {
        radius, blur: Math.round(radius * 0.7), max: heatMax, minOpacity: 0.3, maxZoom: 17,
      }).addTo(m);
    } else {
      const hv = heat.filter((_, i) => parseTs(points[i].recorded_at) <= curTs && visible(points[i]));
      overlay.current = L.layerGroup(
        hv.map((h) => L.circleMarker([h[0], h[1]],
          { radius: 4 + 10 * h[2], stroke: false, fillColor: "#ff7043", fillOpacity: 0.35 })),
      ).addTo(m);
    }
  }, [points, mode, curTs, heat, heatLevel, personOf, hidden, zoomLevel]);

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
      {presentPeople.length > 1 && (
        <div className="people-filter">
          {presentPeople.map((p) => {
            const off = hidden.has(p.id);
            return (
              <button key={p.id} className={"people-chip" + (off ? " off" : "")}
                      onClick={() => setHidden((h) => { const n = new Set(h); n.has(p.id) ? n.delete(p.id) : n.add(p.id); return n; })}>
                <i className="legend-dot" style={{ background: p.color }} />{p.name}
              </button>
            );
          })}
        </div>
      )}
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
                <li key={p.id} className={editing?.id === p.id ? "editing" : undefined}>
                  {editing?.id === p.id ? (
                    <div className="place-edit">
                      <input className="place-edit-name" value={editing.name} autoFocus
                             placeholder="Place name"
                             onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                             onKeyDown={(e) => { if (e.key === "Enter") saveEditing(); if (e.key === "Escape") setEditing(null); }} />
                      <div className="place-edit-fence">
                        <span className="place-edit-label">Fence</span>
                        <input type="range" min={20} max={2000} step={10} value={Math.min(editing.radius, 2000)}
                               onChange={(e) => setEditing({ ...editing, radius: Number(e.target.value) })} />
                        <input type="number" className="place-edit-num" min={20} max={20000} value={editing.radius}
                               onChange={(e) => setEditing({ ...editing, radius: Math.max(20, Math.min(Number(e.target.value) || 20, 20000)) })} />
                        <span className="place-edit-unit">m</span>
                      </div>
                      <div className="place-edit-actions">
                        <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
                        <button className="primary" onClick={saveEditing}>Save</button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <button className="place-go" onClick={() => map.current?.setView([p.lat, p.lon], 16)}>{p.name}</button>
                      <span className="place-r">{p.radius_m} m</span>
                      <button className="place-del" title="Open notes" onClick={() => openPlaceNotes(p.id)}>📝</button>
                      <button className="place-del" title="Edit name & fence size"
                              onClick={() => setEditing({ id: p.id, name: p.name, radius: p.radius_m })}>✎</button>
                      <button className="place-del" title="Delete" onClick={() => deletePlace(p.id).then(loadPlaces)}>✕</button>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <div className="map-play">
        <button className="icon-btn" disabled={timeline.length < 2}
                onClick={() => { if (idx >= timeline.length - 1) setIdx(0); followingRef.current = false; setPlaying((p) => !p); }}>
          {playing ? "❚❚" : "▶"}
        </button>
        <input type="range" min={0} max={Math.max(0, timeline.length - 1)} value={idx}
               disabled={timeline.length < 2}
               onChange={(e) => { const v = +e.target.value; setPlaying(false); setIdx(v);
                                  followingRef.current = v >= timeline.length - 1; }} />
        <span className="map-time">
          {loading ? "Loading…" : timeline.length ? `${fmt(curTs)}  ·  ${idx + 1}/${timeline.length}` : "No location data yet"}
        </span>
      </div>
    </div>
  );
}
