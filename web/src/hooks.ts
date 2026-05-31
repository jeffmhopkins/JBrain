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
