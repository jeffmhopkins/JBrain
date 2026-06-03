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

// The opt-in location flag is shared by two independent hooks (useGeo's toggle/UI
// and useLocationTrail). Reading localStorage isn't reactive, so the toggle
// broadcasts this event; both hooks re-sync on it (and on cross-tab `storage`),
// making a flip take effect instantly in both directions.
const GEO_EVENT = "jbrain-geo-changed";
const geoOn = () => localStorage.getItem("jbrain_geo") === "1";

/** Subscribe a setter to geo-flag changes (same-tab event + cross-tab storage). */
function useGeoFlagSync(setOn: (on: boolean) => void): void {
  useEffect(() => {
    const sync = () => setOn(geoOn());
    window.addEventListener(GEO_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(GEO_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, [setOn]);
}

/** Opt-in geolocation. Persists the toggle; watches position only while enabled. */
export function useGeo() {
  const [enabled, setEnabled] = useState(geoOn);
  const [coords, setCoords] = useState<Coords | null>(null);
  useGeoFlagSync(setEnabled);   // stay in sync if toggled elsewhere

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
    const next = !geoOn();
    localStorage.setItem("jbrain_geo", next ? "1" : "0");
    setEnabled(next);
    window.dispatchEvent(new Event(GEO_EVENT));   // tell the trail (and any other view) right away
  }
  return { enabled, coords, toggle };
}

// The continuous foreground trail used to live here (useLocationTrail), posting to
// /api/locations while the app was open. The native tracker app now owns the trail,
// so the PWA no longer logs location — it only STAMPS new posts via useGeo's coords.

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
