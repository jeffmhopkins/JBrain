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
   *  geolocation is unavailable, or the request is denied/times out.
   *
   *  `maxWait` caps how long the caller is willing to block: if no fix arrives within
   *  that window we resolve null and let the post fire un-stamped. The underlying
   *  request keeps running and may warm the browser's 60s cache so the NEXT post gets
   *  a fix instantly. This keeps a cold GPS radio from freezing the UI for the full
   *  10s timeout. Omit `maxWait` to wait the full timeout for a guaranteed-if-available
   *  fix (unchanged legacy behaviour). The fix is best-effort metadata, not the payload. */
  function getCoords(maxWait?: number): Promise<Coords | null> {
    if (!enabled || !("geolocation" in navigator)) return Promise.resolve(null);
    return new Promise((resolve) => {
      let done = false;
      let timer: number | undefined;
      const finish = (c: Coords | null) => {
        if (done) return;
        done = true;
        if (timer !== undefined) window.clearTimeout(timer);
        resolve(c);
      };
      if (maxWait != null && maxWait > 0) timer = window.setTimeout(() => finish(null), maxWait);
      navigator.geolocation.getCurrentPosition(
        (p) => finish({ lat: +p.coords.latitude.toFixed(6), lon: +p.coords.longitude.toFixed(6) }),
        () => finish(null),
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

/** Re-render on an interval while `active`, returning Date.now() sampled each tick.
 *  Drives the live "running for Xs" indicators in the watch modal and update console;
 *  when inactive it holds no timer, so an idle/closed modal stays cheap. */
export function useNowTick(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [active, intervalMs]);
  return now;
}

// On-device text-to-speech via the Web Speech API. Voice + speed persist per device
// (localStorage), so the choice you make in Settings is reused everywhere speech is
// used later (e.g. reading Assisted replies aloud). No server, no network.
const TTS_VOICE_KEY = "jbrain_tts_voice";   // stored by voiceURI (stable id)
const TTS_RATE_KEY = "jbrain_tts_rate";

/** Manage TTS voices + speed and speak/stop on demand. Returns null-safe state when
 *  the browser has no speechSynthesis so callers can disable their UI. */
export function useTts() {
  const supported = typeof window !== "undefined" && "speechSynthesis" in window;
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);
  const [voiceURI, setVoiceURI] = useState<string>(() => localStorage.getItem(TTS_VOICE_KEY) || "");
  const [rate, setRateState] = useState<number>(() => {
    const r = parseFloat(localStorage.getItem(TTS_RATE_KEY) || "1.3");
    return Number.isFinite(r) ? Math.min(2, Math.max(0.5, r)) : 1.3;
  });
  const [speaking, setSpeaking] = useState(false);

  // getVoices() is async on most browsers — it's empty until `voiceschanged` fires.
  useEffect(() => {
    if (!supported) return;
    const synth = window.speechSynthesis;
    const load = () => setVoices(synth.getVoices());
    load();
    synth.addEventListener("voiceschanged", load);
    return () => synth.removeEventListener("voiceschanged", load);
  }, [supported]);

  // Stop any in-flight speech when the consuming component unmounts.
  useEffect(() => () => { if (supported) window.speechSynthesis.cancel(); }, [supported]);

  function setVoice(uri: string) {
    setVoiceURI(uri);
    localStorage.setItem(TTS_VOICE_KEY, uri);
  }
  function setRate(r: number) {
    const clamped = Math.min(2, Math.max(0.5, r));
    setRateState(clamped);
    localStorage.setItem(TTS_RATE_KEY, String(clamped));
  }
  function stop() {
    if (!supported) return;
    window.speechSynthesis.cancel();
    setSpeaking(false);
  }
  /** Speak `text` with the saved voice + speed. No-ops (returns false) if unsupported. */
  function speak(text: string): boolean {
    if (!supported || !text.trim()) return false;
    const synth = window.speechSynthesis;
    synth.cancel();   // clear any stale/queued utterance first
    const utter = new SpeechSynthesisUtterance(text);
    const v = voices.find((x) => x.voiceURI === voiceURI);
    if (v) utter.voice = v;
    utter.rate = rate;
    utter.onend = () => setSpeaking(false);
    utter.onerror = () => setSpeaking(false);
    setSpeaking(true);
    synth.speak(utter);
    return true;
  }

  return { supported, voices, voiceURI, setVoice, rate, setRate, speaking, speak, stop };
}

// Global "read replies aloud" flag, toggled from the top bar and read by the Chat
// page. Mirrors the geo-flag pattern so a flip takes effect everywhere instantly.
const TTS_ON_EVENT = "jbrain-tts-enabled-changed";
const ttsOn = () => localStorage.getItem("jbrain_tts_enabled") === "1";

function useTtsEnabledSync(setOn: (on: boolean) => void): void {
  useEffect(() => {
    const sync = () => setOn(ttsOn());
    window.addEventListener(TTS_ON_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(TTS_ON_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, [setOn]);
}

/** Whether Assisted replies should be spoken aloud. Persisted per device. */
export function useTtsEnabled() {
  const [enabled, setEnabled] = useState(ttsOn);
  useTtsEnabledSync(setEnabled);
  function toggle() {
    const next = !ttsOn();
    localStorage.setItem("jbrain_tts_enabled", next ? "1" : "0");
    setEnabled(next);
    window.dispatchEvent(new Event(TTS_ON_EVENT));
  }
  return { enabled, toggle };
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
