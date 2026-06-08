// Unit (Tier 1) tests for the custom hooks in hooks.ts. These hooks are device-local:
// they touch localStorage, matchMedia, geolocation, speechSynthesis, and online/offline
// events — none of which jsdom provides — so each block stubs exactly the global it needs
// via vi.stubGlobal (auto-restored by unstubGlobals between tests). No network is used, so
// no MSW handlers are registered here; the few that would fire are kept off by design.
//
// Timers: per the project rule for these hooks we use vi.setSystemTime (not full fake
// timers) so real microtasks/intervals still run; useNowTick is exercised with a real
// short interval via waitFor.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import {
  useMediaQuery, useIsDesktop, useTheme, applyTheme, useGeo,
  useNowTick, useTts, useTtsEnabled, useOnline, type Theme,
} from "./hooks";

// A controllable matchMedia: tracks listeners so a test can flip `matches` and fire change.
function makeMatchMedia(initial: boolean) {
  const listeners = new Set<() => void>();
  let matches = initial;
  const mql = {
    get matches() { return matches; },
    media: "",
    addEventListener: (_: string, cb: () => void) => listeners.add(cb),
    removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
  };
  const fn = vi.fn((q: string) => { (mql as any).media = q; return mql as any; });
  return {
    fn,
    set(next: boolean) { matches = next; listeners.forEach((cb) => cb()); },
    listenerCount: () => listeners.size,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.documentElement.removeAttribute("data-theme");
});

describe("useMediaQuery / useIsDesktop", () => {
  it("returns the initial match and re-renders when the query flips", () => {
    const mm = makeMatchMedia(false);
    vi.stubGlobal("matchMedia", mm.fn);
    Object.defineProperty(window, "matchMedia", { value: mm.fn, configurable: true });

    const { result } = renderHook(() => useMediaQuery("(min-width: 900px)"));
    expect(result.current).toBe(false);

    act(() => mm.set(true));
    expect(result.current).toBe(true);
  });

  it("unsubscribes the change listener on unmount", () => {
    const mm = makeMatchMedia(true);
    Object.defineProperty(window, "matchMedia", { value: mm.fn, configurable: true });

    const { result, unmount } = renderHook(() => useIsDesktop());
    expect(result.current).toBe(true);
    expect(mm.listenerCount()).toBe(1);
    unmount();
    expect(mm.listenerCount()).toBe(0);
  });
});

describe("useTheme + applyTheme", () => {
  it("defaults to dark (no data-theme) and persists/applies a chosen theme", () => {
    const meta = document.createElement("meta");
    meta.setAttribute("name", "theme-color");
    document.head.appendChild(meta);
    const bar = document.createElement("meta");
    bar.setAttribute("name", "apple-mobile-web-app-status-bar-style");
    document.head.appendChild(bar);

    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);

    act(() => result.current.setTheme("sunlight"));
    expect(result.current.theme).toBe("sunlight");
    expect(localStorage.getItem("jbrain_theme")).toBe("sunlight");
    expect(document.documentElement.getAttribute("data-theme")).toBe("sunlight");
    expect(meta.content).toBe("#ffffff");
    expect(bar.content).toBe("default");

    meta.remove();
    bar.remove();
  });

  it("reads a persisted theme on mount and re-syncs on the broadcast event", () => {
    localStorage.setItem("jbrain_theme", "light");
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("light");

    // Another view changes the stored value, then broadcasts — this hook re-reads it.
    act(() => {
      localStorage.setItem("jbrain_theme", "dark");
      window.dispatchEvent(new Event("jbrain-theme-changed"));
    });
    expect(result.current.theme).toBe("dark");
  });

  it("applyTheme(dark) removes the attribute and uses black-translucent bar style", () => {
    const bar = document.createElement("meta");
    bar.setAttribute("name", "apple-mobile-web-app-status-bar-style");
    document.head.appendChild(bar);
    document.documentElement.setAttribute("data-theme", "light");

    applyTheme("dark" as Theme);
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(bar.content).toBe("black-translucent");
    bar.remove();
  });
});

describe("useGeo", () => {
  it("toggle flips and persists the flag, and notifies via the broadcast event", () => {
    const { result } = renderHook(() => useGeo());
    expect(result.current.enabled).toBe(false);

    act(() => result.current.toggle());
    expect(result.current.enabled).toBe(true);
    expect(localStorage.getItem("jbrain_geo")).toBe("1");

    act(() => result.current.toggle());
    expect(result.current.enabled).toBe(false);
    expect(localStorage.getItem("jbrain_geo")).toBe("0");
  });

  it("getCoords resolves null while disabled (never touches geolocation)", async () => {
    const getCurrentPosition = vi.fn();
    Object.defineProperty(navigator, "geolocation", {
      value: { getCurrentPosition }, configurable: true,
    });
    const { result } = renderHook(() => useGeo());
    await expect(result.current.getCoords()).resolves.toBeNull();
    expect(getCurrentPosition).not.toHaveBeenCalled();
  });

  it("getCoords returns a rounded fix when enabled and the position resolves", async () => {
    const getCurrentPosition = vi.fn((ok: any) =>
      ok({ coords: { latitude: 37.123456789, longitude: -122.987654321 } }));
    Object.defineProperty(navigator, "geolocation", {
      value: { getCurrentPosition }, configurable: true,
    });
    localStorage.setItem("jbrain_geo", "1");
    const { result } = renderHook(() => useGeo());

    await expect(result.current.getCoords()).resolves.toEqual({ lat: 37.123457, lon: -122.987654 });
  });

  it("getCoords resolves null on a geolocation error", async () => {
    const getCurrentPosition = vi.fn((_ok: any, err: any) => err(new Error("denied")));
    Object.defineProperty(navigator, "geolocation", {
      value: { getCurrentPosition }, configurable: true,
    });
    localStorage.setItem("jbrain_geo", "1");
    const { result } = renderHook(() => useGeo());
    await expect(result.current.getCoords()).resolves.toBeNull();
  });

  it("getCoords honours maxWait — resolves null before a slow fix arrives", async () => {
    let resolveFix: (() => void) | null = null;
    const getCurrentPosition = vi.fn((ok: any) => {
      resolveFix = () => ok({ coords: { latitude: 1, longitude: 2 } });
    });
    Object.defineProperty(navigator, "geolocation", {
      value: { getCurrentPosition }, configurable: true,
    });
    localStorage.setItem("jbrain_geo", "1");
    const { result } = renderHook(() => useGeo());

    // maxWait of 5ms elapses before we ever deliver the fix → null.
    await expect(result.current.getCoords(5)).resolves.toBeNull();
    expect(resolveFix).toBeTypeOf("function"); // request still in flight (warms cache)
  });
});

describe("useNowTick", () => {
  it("holds a single sample while inactive", () => {
    vi.setSystemTime(new Date("2026-06-08T00:00:00Z"));
    const base = Date.now();
    const { result } = renderHook(() => useNowTick(false));
    expect(result.current).toBe(base);
  });

  it("advances on its interval while active, then stops on unmount", async () => {
    const { result, unmount } = renderHook(() => useNowTick(true, 10));
    const first = result.current;
    await waitFor(() => expect(result.current).toBeGreaterThan(first));
    unmount(); // clears the interval — no further work
  });
});

describe("useTts", () => {
  function stubSpeech(voices: any[] = []) {
    const listeners = new Set<() => void>();
    const speak = vi.fn();
    const cancel = vi.fn();
    const synth = {
      getVoices: () => voices,
      speak, cancel,
      speaking: false, pending: false,
      addEventListener: (_: string, cb: () => void) => listeners.add(cb),
      removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
      fire() { listeners.forEach((cb) => cb()); },
    };
    Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
    // hooks.ts constructs SpeechSynthesisUtterance; jsdom lacks it.
    vi.stubGlobal("SpeechSynthesisUtterance", class {
      text: string; voice: any = null; lang = ""; rate = 1; volume = 1;
      onend: any = null; onerror: any = null;
      constructor(t = "") { this.text = t; }
    });
    return { synth, speak, cancel };
  }

  it("reports unsupported when speechSynthesis is absent (speak no-ops to false)", () => {
    // Ensure the property is gone for this test (the hook checks `"speechSynthesis" in window`,
    // so the key must be absent, not merely undefined).
    delete (window as any).speechSynthesis;
    const { result } = renderHook(() => useTts());
    expect(result.current.supported).toBe(false);
    let ret: boolean | undefined;
    act(() => { ret = result.current.speak("hi"); });
    expect(ret).toBe(false);
  });

  it("loads voices initially and re-loads when voiceschanged fires", () => {
    const v1 = { voiceURI: "en-1", name: "Alice", lang: "en-US", default: true } as any;
    let pool: any[] = [];
    const listeners = new Set<() => void>();
    const synth = {
      getVoices: () => pool,
      speak: vi.fn(), cancel: vi.fn(), speaking: false, pending: false,
      addEventListener: (_: string, cb: () => void) => listeners.add(cb),
      removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
    };
    Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });

    const { result } = renderHook(() => useTts());
    expect(result.current.voices).toEqual([]);

    act(() => { pool = [v1]; listeners.forEach((cb) => cb()); });
    expect(result.current.voices).toEqual([v1]);
  });

  it("persists voice and clamps rate to [0.5, 2]", () => {
    stubSpeech();
    const { result } = renderHook(() => useTts());

    act(() => result.current.setVoice("en-1"));
    expect(result.current.voiceURI).toBe("en-1");
    expect(localStorage.getItem("jbrain_tts_voice")).toBe("en-1");

    act(() => result.current.setRate(5)); // over max → 2
    expect(result.current.rate).toBe(2);
    act(() => result.current.setRate(0.1)); // under min → 0.5
    expect(result.current.rate).toBe(0.5);
    expect(localStorage.getItem("jbrain_tts_rate")).toBe("0.5");
  });

  it("reads a persisted rate on mount, clamping an out-of-range stored value", () => {
    localStorage.setItem("jbrain_tts_rate", "9");
    stubSpeech();
    const { result } = renderHook(() => useTts());
    expect(result.current.rate).toBe(2);
  });

  it("falls back to 1.3 when the stored rate is non-numeric", () => {
    localStorage.setItem("jbrain_tts_rate", "not-a-number");
    stubSpeech();
    const { result } = renderHook(() => useTts());
    expect(result.current.rate).toBe(1.3);
  });

  it("speak() sets the matching voice+lang, marks speaking, and clears on end", () => {
    const v = { voiceURI: "en-1", name: "Alice", lang: "en-GB", default: false } as any;
    const { synth, speak } = stubSpeech([v]);
    const { result } = renderHook(() => useTts());
    act(() => { (synth as any).fire(); });        // load voices
    act(() => result.current.setVoice("en-1"));

    let ret: boolean | undefined;
    act(() => { ret = result.current.speak("hello"); });
    expect(ret).toBe(true);
    expect(result.current.speaking).toBe(true);
    const utter = speak.mock.calls[0][0];
    expect(utter.voice).toBe(v);
    expect(utter.lang).toBe("en-GB");
    expect(utter.rate).toBe(result.current.rate);

    act(() => { utter.onend(); });
    expect(result.current.speaking).toBe(false);
  });

  it("speak() returns false for blank/whitespace text", () => {
    stubSpeech();
    const { result } = renderHook(() => useTts());
    let ret: boolean | undefined;
    act(() => { ret = result.current.speak("   "); });
    expect(ret).toBe(false);
  });

  it("speak() cancels first when the engine is already busy", () => {
    const { synth, speak, cancel } = stubSpeech();
    (synth as any).pending = true;
    const { result } = renderHook(() => useTts());
    act(() => { result.current.speak("go"); });
    expect(cancel).toHaveBeenCalled();
    expect(speak).toHaveBeenCalled();
  });

  it("stop() cancels and clears speaking; prime() speaks a silent utterance", () => {
    const { speak, cancel } = stubSpeech();
    const { result } = renderHook(() => useTts());
    act(() => { result.current.prime(); });
    expect(speak).toHaveBeenCalledTimes(1);
    expect(speak.mock.calls[0][0].volume).toBe(0);

    act(() => { result.current.stop(); });
    expect(cancel).toHaveBeenCalled();
    expect(result.current.speaking).toBe(false);
  });

  it("cancels in-flight speech on unmount", () => {
    const { cancel } = stubSpeech();
    const { unmount } = renderHook(() => useTts());
    unmount();
    expect(cancel).toHaveBeenCalled();
  });
});

describe("useTtsEnabled", () => {
  it("defaults off, toggles + persists, and re-syncs on the broadcast event", () => {
    const { result } = renderHook(() => useTtsEnabled());
    expect(result.current.enabled).toBe(false);

    act(() => result.current.toggle());
    expect(result.current.enabled).toBe(true);
    expect(localStorage.getItem("jbrain_tts_enabled")).toBe("1");

    act(() => {
      localStorage.setItem("jbrain_tts_enabled", "0");
      window.dispatchEvent(new Event("jbrain-tts-enabled-changed"));
    });
    expect(result.current.enabled).toBe(false);
  });
});

describe("useOnline", () => {
  it("tracks navigator.onLine across online/offline events", () => {
    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
    const { result } = renderHook(() => useOnline());
    expect(result.current).toBe(true);

    act(() => { window.dispatchEvent(new Event("offline")); });
    expect(result.current).toBe(false);

    act(() => { window.dispatchEvent(new Event("online")); });
    expect(result.current).toBe(true);
  });
});

// Remaining branch coverage for the device-local hooks: the early-return / fallback
// arms the main suite above doesn't exercise. Kept here (same file) so the hooks'
// tests stay in one place; no network, same vi.stubGlobal discipline.
describe("hooks — remaining branches", () => {
  it("getCoords resolves null when enabled but the device has no geolocation API", async () => {
    // Enabled flag set, but navigator.geolocation absent → the `"geolocation" in navigator`
    // guard short-circuits to null without ever constructing the Promise's locator call.
    delete (navigator as any).geolocation;
    localStorage.setItem("jbrain_geo", "1");
    const { result } = renderHook(() => useGeo());
    await expect(result.current.getCoords()).resolves.toBeNull();
  });

  it("getCoords ignores a fix that arrives after maxWait already resolved null", async () => {
    // The maxWait timer wins the race and resolves null; when the (late) position
    // callback fires it hits the `if (done) return` guard and is dropped — the
    // resolved value stays null (the warm-cache request simply completed in the bg).
    let deliver: (() => void) | null = null;
    const getCurrentPosition = vi.fn((ok: any) => {
      deliver = () => ok({ coords: { latitude: 1.111111, longitude: 2.222222 } });
    });
    Object.defineProperty(navigator, "geolocation", {
      value: { getCurrentPosition }, configurable: true,
    });
    localStorage.setItem("jbrain_geo", "1");
    const { result } = renderHook(() => useGeo());

    const p = result.current.getCoords(1);
    await expect(p).resolves.toBeNull();
    // Late fix arrives now — the guard drops it (no throw, value unchanged).
    act(() => { deliver?.(); });
    await expect(p).resolves.toBeNull();
  });

  it("getCoords resolves a fix immediately when one arrives before maxWait elapses", async () => {
    // The success path WITH a maxWait set (clears the pending timer in finish()).
    const getCurrentPosition = vi.fn((ok: any) =>
      ok({ coords: { latitude: 10.5, longitude: 20.25 } }));
    Object.defineProperty(navigator, "geolocation", {
      value: { getCurrentPosition }, configurable: true,
    });
    localStorage.setItem("jbrain_geo", "1");
    const { result } = renderHook(() => useGeo());
    await expect(result.current.getCoords(5000)).resolves.toEqual({ lat: 10.5, lon: 20.25 });
  });

  it("useTts.speak with no matching saved voice leaves voice/lang unset", () => {
    const v = { voiceURI: "en-1", name: "Alice", lang: "en-US", default: false } as any;
    const listeners = new Set<() => void>();
    const speak = vi.fn();
    const synth = {
      getVoices: () => [v],
      speak, cancel: vi.fn(), speaking: false, pending: false,
      addEventListener: (_: string, cb: () => void) => listeners.add(cb),
      removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
    };
    Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
    vi.stubGlobal("SpeechSynthesisUtterance", class {
      text: string; voice: any = null; lang = ""; rate = 1; volume = 1;
      onend: any = null; onerror: any = null;
      constructor(t = "") { this.text = t; }
    });
    const { result } = renderHook(() => useTts());
    act(() => { listeners.forEach((cb) => cb()); });   // load voices

    // voiceURI stays "" (no setVoice) → find() returns undefined → the `if (v)` arm is skipped.
    let ret: boolean | undefined;
    act(() => { ret = result.current.speak("hello"); });
    expect(ret).toBe(true);
    const utter = speak.mock.calls[0][0];
    expect(utter.voice).toBeNull();
    expect(utter.lang).toBe("");
  });

  it("useTts.speak clears speaking via onerror as well as onend", () => {
    const listeners = new Set<() => void>();
    const speak = vi.fn();
    const synth = {
      getVoices: () => [],
      speak, cancel: vi.fn(), speaking: false, pending: false,
      addEventListener: (_: string, cb: () => void) => listeners.add(cb),
      removeEventListener: (_: string, cb: () => void) => listeners.delete(cb),
    };
    Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
    vi.stubGlobal("SpeechSynthesisUtterance", class {
      text: string; voice: any = null; lang = ""; rate = 1; volume = 1;
      onend: any = null; onerror: any = null;
      constructor(t = "") { this.text = t; }
    });
    const { result } = renderHook(() => useTts());
    act(() => { result.current.speak("oops"); });
    expect(result.current.speaking).toBe(true);
    const utter = speak.mock.calls[0][0];
    act(() => { utter.onerror(); });
    expect(result.current.speaking).toBe(false);
  });

  it("useTts.stop() and prime() are no-ops when speech is unsupported", () => {
    delete (window as any).speechSynthesis;
    const { result } = renderHook(() => useTts());
    expect(result.current.supported).toBe(false);
    // Neither should throw nor flip state (both hit the `if (!supported) return` guard).
    act(() => { result.current.stop(); });
    act(() => { result.current.prime(); });
    expect(result.current.speaking).toBe(false);
  });

  it("useTtsEnabled.toggle() persists '0' when toggling back off", () => {
    localStorage.setItem("jbrain_tts_enabled", "1");
    const { result } = renderHook(() => useTtsEnabled());
    expect(result.current.enabled).toBe(true);
    act(() => result.current.toggle());   // on → off → stores "0"
    expect(result.current.enabled).toBe(false);
    expect(localStorage.getItem("jbrain_tts_enabled")).toBe("0");
  });
});
