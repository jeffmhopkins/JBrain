import { useEffect, useState } from "react";

/** Reactively track a media query (used to swap the app shell on desktop vs phone). */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => typeof window !== "undefined" && window.matchMedia(query).matches,
  );
  useEffect(() => {
    const mql = window.matchMedia(query);
    const handler = () => setMatches(mql.matches);
    handler();
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, [query]);
  return matches;
}

export const useIsDesktop = () => useMediaQuery("(min-width: 900px)");

export interface Coords { lat: number; lon: number; }

/** Opt-in geolocation. Persists the toggle; watches position only while enabled. */
export function useGeo() {
  const [enabled, setEnabled] = useState(() => localStorage.getItem("jbrain_geo") === "1");
  const [coords, setCoords] = useState<Coords | null>(null);

  useEffect(() => {
    if (!enabled || !("geolocation" in navigator)) { setCoords(null); return; }
    const id = navigator.geolocation.watchPosition(
      (p) => setCoords({
        lat: +p.coords.latitude.toFixed(6),
        lon: +p.coords.longitude.toFixed(6),
      }),
      () => setCoords(null),
      { enableHighAccuracy: false, maximumAge: 60000, timeout: 10000 },
    );
    return () => navigator.geolocation.clearWatch(id);
  }, [enabled]);

  function toggle() {
    setEnabled((e) => {
      const next = !e;
      localStorage.setItem("jbrain_geo", next ? "1" : "0");
      return next;
    });
  }
  return { enabled, coords, toggle };
}

function _haversineM(aLat: number, aLon: number, bLat: number, bLon: number): number {
  const R = 6371000, rad = Math.PI / 180;
  const dLat = (bLat - aLat) * rad, dLon = (bLon - aLon) * rad;
  const s = Math.sin(dLat / 2) ** 2 +
    Math.cos(aLat * rad) * Math.cos(bLat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
}

/** Foreground location trail: while the app is open AND location is enabled, post a
 * fix to /api/locations when >=100 m moved OR >=60 min elapsed. Mounted ONCE (in
 * Shell). A PWA can only do this while open; background tracking is the watch app. */
export function useLocationTrail(): void {
  useEffect(() => {
    if (localStorage.getItem("jbrain_geo") !== "1" || !("geolocation" in navigator)) return;
    let alive = true;
    let last: { lat: number; lon: number; t: number } | null = null;

    async function tick() {
      navigator.geolocation.getCurrentPosition(
        (p) => {
          if (!alive) return;
          const lat = +p.coords.latitude.toFixed(6);
          const lon = +p.coords.longitude.toFixed(6);
          const now = Date.now();
          if (last && _haversineM(last.lat, last.lon, lat, lon) < 100 && (now - last.t) / 60000 < 60) return;
          import("./api").then(({ postLocation }) =>
            postLocation(lat, lon, p.coords.accuracy)
              .then(() => { if (alive) last = { lat, lon, t: now }; })   // only advance on success → failed posts retry
              .catch(() => {}),
          );
        },
        () => {},
        { enableHighAccuracy: false, maximumAge: 60000, timeout: 15000 },
      );
    }

    tick();                                       // once on open
    const id = window.setInterval(tick, 5 * 60 * 1000);   // re-check every 5 min while open
    return () => { alive = false; window.clearInterval(id); };
  }, []);
}

/** Track online/offline so the UI can show a banner and gate writes. */
export function useOnline(): boolean {
  const [online, setOnline] = useState(() => navigator.onLine);
  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);
  return online;
}
