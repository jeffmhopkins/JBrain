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
 * fix to /api/locations on the 100 m / 60 min rule. Polls ADAPTIVELY — every 15 s
 * while you're moving (covering ground), backing off to every 5 min when idle — so
 * a trip is captured densely without draining battery sitting still. Mounted ONCE
 * (in Shell). A PWA can only do this while open; background is the watch app. */
export function useLocationTrail(): void {
  useEffect(() => {
    if (localStorage.getItem("jbrain_geo") !== "1" || !("geolocation" in navigator)) return;
    const FAST = 15 * 1000, IDLE = 5 * 60 * 1000, GRACE = 2 * 60 * 1000;
    let alive = true;
    let timer: number | undefined;
    let lastPosted: { lat: number; lon: number; t: number } | null = null;   // mirrors the server dedup
    let lastSample: { lat: number; lon: number } | null = null;              // for motion detection

    const clear = () => { if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; } };
    const schedule = (delay: number) => { clear(); if (alive) timer = window.setTimeout(tick, delay); };

    function tick() {
      navigator.geolocation.getCurrentPosition(
        (p) => {
          if (!alive) return;
          const lat = +p.coords.latitude.toFixed(6);
          const lon = +p.coords.longitude.toFixed(6);
          const now = Date.now();
          // "Moving" = jumped >=100 m since the last sample, OR we stored a point very
          // recently (still on a journey, crossing 100 m markers). Either → poll fast.
          const moving =
            (!!lastSample && _haversineM(lastSample.lat, lastSample.lon, lat, lon) >= 100) ||
            (!!lastPosted && now - lastPosted.t < GRACE);
          lastSample = { lat, lon };
          // Post on the same 100 m / 60 min rule the server enforces.
          if (!lastPosted || _haversineM(lastPosted.lat, lastPosted.lon, lat, lon) >= 100 || (now - lastPosted.t) / 60000 >= 60) {
            import("./api").then(({ postLocation }) =>
              postLocation(lat, lon, p.coords.accuracy)
                .then(() => { if (alive) lastPosted = { lat, lon, t: now }; })   // only advance on success → failed posts retry
                .catch(() => {}),
            );
          }
          schedule(moving ? FAST : IDLE);
        },
        () => schedule(IDLE),   // on error, back off
        { enableHighAccuracy: false, maximumAge: 15000, timeout: 15000 },
      );
    }

    // A PWA's timers get suspended/throttled while backgrounded (locked screen, app
    // switch), so without this the loop sits on a stale, late-firing timer when you
    // return. Pause while hidden; take a FRESH fix the moment we're visible again.
    const onVisibility = () => {
      if (!alive) return;
      if (document.visibilityState === "visible") tick();   // immediate refresh + reschedule
      else clear();                                          // pause while backgrounded
    };
    document.addEventListener("visibilitychange", onVisibility);

    if (document.visibilityState === "visible") tick();      // start now if foreground
    return () => {
      alive = false;
      clear();
      document.removeEventListener("visibilitychange", onVisibility);
    };
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
