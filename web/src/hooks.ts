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

/** Opt-in geolocation. Persists the toggle. Acquires GPS ONLY on demand (one fix at
 * post time via getCoords) — never a continuous watch while the app is open. */
export function useGeo() {
  const [enabled, setEnabled] = useState(geoOn);
  useGeoFlagSync(setEnabled);   // stay in sync if toggled elsewhere

  /** One-shot fix, taken at the moment of posting. Resolves null if the toggle is off,
   *  geolocation is unavailable, or the request is denied/times out. */
  function getCoords(): Promise<Coords | null> {
    if (!enabled || !("geolocation" in navigator)) return Promise.resolve(null);
    return new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (p) => resolve({ lat: +p.coords.latitude.toFixed(6), lon: +p.coords.longitude.toFixed(6) }),
        () => resolve(null),
        { enableHighAccuracy: false, maximumAge: 60000, timeout: 10000 },
      );
    });
  }

  function toggle() {
    const next = !geoOn();
    localStorage.setItem("jbrain_geo", next ? "1" : "0");
    setEnabled(next);
    window.dispatchEvent(new Event(GEO_EVENT));   // notify any other view right away
  }
  return { enabled, getCoords, toggle };
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
