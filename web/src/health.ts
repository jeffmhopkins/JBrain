// Client health model: a module-level singleton store (subscribed via
// useSyncExternalStore) that merges
//   (1) server-DECLARED capabilities (from /api/auth/verify on boot/login and the
//       /api/system/status poll), and
//   (2) locally-OBSERVED outcomes of real traffic (5xx/network/stall/LLM errors —
//       wired in Phase 4).
// It is intentionally NOT threaded through React context, so a 20s poll never
// re-renders the whole authed tree — components subscribe to the slices they use.
//
// Invariants:
//  - Declared state WINS; observed signals can only DOWNGRADE (and self-heal).
//  - This store NEVER logs the user out. "needs-auth" only tints the dot; the sole
//    logout path stays in App.tsx (a real 401 from /api/auth/verify).
import { useSyncExternalStore } from "react";
import { getAccessKey, getStatus, type StatusResult } from "./api";

export type CapState =
  | "unknown" | "warming" | "ready" | "failed" | "unavailable" | "absent" | "degraded"
  | "pulling";   // local_llm: model download in progress

// Reachability of OUR server, distinct from navigator.onLine (the browser's link).
export type Reachability = "online" | "server-unreachable" | "browser-offline";
// Server auth/answer state from the last poll.
export type ServerHealth = "ok" | "unreachable" | "needs-auth" | "unknown";

interface Cap { state: CapState; last_error?: string | null; since?: number }
export interface Capabilities {
  // llm.state is the AUTHORITATIVE has_credentials() rollup ("ready"|"absent"|"degraded").
  // providers is INFORMATIONAL ONLY (ModelPicker hint) — it never gates.
  llm: Cap & { verified?: boolean | null; providers?: { anthropic: boolean; xai: boolean; local?: boolean } };
  // Optional: present only when the server reports the local-LLM subsystem. Informational
  // (a per-tier offload), never an authoritative gate. May carry the configured model id.
  local_llm?: Cap & { model?: string | null };
  embeddings: Cap;
  transcription: Cap & { model?: string | null; compute_type?: string | null };
  push: Cap;
  geocoder: Cap;
  db: Cap;
}

export interface HealthModel {
  reachability: Reachability;
  server: ServerHealth;
  caps?: Capabilities;          // last server-declared caps, with the llm observed-overlay applied
  lastOkAt: number | null;      // last server byte (any HTTP response, incl. 5xx) or poll success
  observed: Observed;
}

interface Observed {
  last5xxAt?: number;
  lastNetErrAt?: number;
  lastStallAt?: number;
  llmFailAt?: number;
  llmOkAt?: number;
}

export type ObservedEvent =
  | { kind: "http"; status: number }
  | { kind: "neterr" }
  | { kind: "stall" }
  | { kind: "llm-fail" }
  | { kind: "llm-ok" };

// A server byte more recent than this is treated as "reachable". A net error with
// no server byte inside this window flips reachability to server-unreachable.
const REACH_WINDOW_MS = 8000;
// An LLM failure within this window (with no newer success) renders llm as degraded.
const LLM_DEGRADE_MS = 60000;

// Indirection so tests can pin time deterministically if needed (vitest fake timers
// also work, since they mock Date.now()).
let _now = () => Date.now();

interface Internal {
  online: boolean;
  serverCaps?: Capabilities;
  pollServer: ServerHealth;     // last poll outcome (ok | unreachable | needs-auth | unknown)
  lastOkAt: number | null;
  observed: Observed;
}

let st: Internal = {
  online: typeof navigator === "undefined" ? true : navigator.onLine,
  serverCaps: undefined,
  pollServer: "unknown",
  lastOkAt: null,
  observed: {},
};

let current: HealthModel = compute(st);
const listeners = new Set<() => void>();

function compute(s: Internal): HealthModel {
  const now = _now();
  const { observed } = s;
  const lastNetFail = Math.max(observed.lastNetErrAt ?? 0, observed.lastStallAt ?? 0);

  // --- reachability ---
  let reachability: Reachability;
  if (!s.online) {
    reachability = "browser-offline";
  } else if (s.pollServer === "unreachable") {
    reachability = "server-unreachable";
  } else if (
    lastNetFail > 0 &&
    now - lastNetFail < REACH_WINDOW_MS &&
    (s.lastOkAt == null || now - s.lastOkAt >= REACH_WINDOW_MS) &&
    lastNetFail >= (s.lastOkAt ?? 0)
  ) {
    // Observed a network failure with no recent server byte → server unreachable,
    // even between polls. A single transient 5xx does NOT trip this (a 5xx is itself
    // a server byte → lastOkAt is fresh).
    reachability = "server-unreachable";
  } else {
    reachability = "online";
  }

  // --- server field ---
  let server: ServerHealth;
  if (!s.online) server = "unknown";
  else if (reachability === "server-unreachable") server = "unreachable";
  else server = s.pollServer;

  // --- caps with the LLM observed overlay (declared wins; observed only downgrades) ---
  let caps = s.serverCaps;
  if (caps) {
    const degraded =
      observed.llmFailAt != null &&
      now - observed.llmFailAt < LLM_DEGRADE_MS &&
      observed.llmFailAt > (observed.llmOkAt ?? 0);
    if (degraded && caps.llm.state === "ready") {
      caps = { ...caps, llm: { ...caps.llm, state: "degraded" } };
    }
  }

  return { reachability, server, caps, lastOkAt: s.lastOkAt, observed };
}

function emit() {
  current = compute(st);
  listeners.forEach((l) => l());
}

// --- store API (also usable outside React) ---------------------------------
export function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}
export function getSnapshot(): HealthModel {
  return current;
}

/** Ingest a /verify or /status envelope (both nest the same `capabilities` object). */
export function ingestVerify(data: any): void {
  if (data?.capabilities) {
    st = { ...st, serverCaps: data.capabilities as Capabilities, pollServer: "ok", lastOkAt: _now() };
    emit();
  }
}

/** Apply the result of a getStatus() poll. Implements reconciliation rule 0. */
export function applyPoll(result: StatusResult): void {
  if (!result.ok) {
    if (st.pollServer !== "unreachable") {        // skip redundant wakeups on repeat failures
      st = { ...st, pollServer: "unreachable" };  // keep last-known caps
      emit();
    }
    return;
  }
  const data = result.data;
  if (data?.capabilities) {
    st = { ...st, serverCaps: data.capabilities as Capabilities, pollServer: "ok", lastOkAt: _now() };
  } else {
    // 200 SKELETON. Rule 0: a stored key that yields only a skeleton no longer
    // verifies → needs-auth (NOT status===401, which a soft-auth route never sends).
    // No stored key (pre-login) → just reachable.
    const needsAuth = !!getAccessKey();
    st = { ...st, pollServer: needsAuth ? "needs-auth" : "ok", lastOkAt: _now() };
  }
  emit();
}

/** Feed an observed outcome of real traffic (wired into api()/streamChat in Phase 4). */
export function report(ev: ObservedEvent): void {
  const now = _now();
  const o = { ...st.observed };
  let lastOkAt = st.lastOkAt;
  switch (ev.kind) {
    case "http":
      lastOkAt = now;                         // any HTTP response proves the server is reachable
      if (ev.status >= 500) o.last5xxAt = now;
      break;
    case "neterr":
      o.lastNetErrAt = now;
      break;
    case "stall":
      o.lastStallAt = now;
      break;
    case "llm-fail":
      o.llmFailAt = now;
      break;
    case "llm-ok":
      o.llmOkAt = now;
      break;
  }
  st = { ...st, observed: o, lastOkAt };
  emit();
}

export function setOnline(online: boolean): void {
  if (st.online !== online) {
    st = { ...st, online };
    emit();
  }
}

/** Test-only: reset all state. */
export function __reset(nowFn?: () => number): void {
  if (nowFn) _now = nowFn;
  st = {
    online: typeof navigator === "undefined" ? true : navigator.onLine,
    serverCaps: undefined,
    pollServer: "unknown",
    lastOkAt: null,
    observed: {},
  };
  emit();
}

// --- React bindings --------------------------------------------------------
export function useHealth<T>(selector: (m: HealthModel) => T): T {
  return useSyncExternalStore(
    subscribe,
    () => selector(current),
    () => selector(current),
  );
}

// Health-poll policy: poll on every authed/KeyEntry route, but NEVER on the public
// /share/:token recipient route (no key; must not poll the owner's server). The path
// here is react-router's basename-STRIPPED pathname, so this holds under any deploy base.
export function pollEnabledForPath(pathname: string): boolean {
  return !pathname.startsWith("/share/");
}

// --- imperative refresh (lets App.tsx connect() force an immediate poll) ----
let _refreshNow: (() => void) | null = null;
export function refreshNow(): void {
  _refreshNow?.();
}

// --- the poller ------------------------------------------------------------
import { useEffect, useRef } from "react";

/** Pure cadence calc (exported for tests): backoff dominates; otherwise 5s while
 * warming, 20s steady. Sequence for steps 1..6: 5,10,20,40,60,60 (s). */
export function pollDelay(backoffSteps: number, warming: boolean): number {
  if (backoffSteps > 0) return Math.min(5000 * 2 ** (backoffSteps - 1), 60000);
  return warming ? 5000 : 20000;
}

/**
 * Drive the live status poll. Mount ONCE above the auth gate (so KeyEntry gets a
 * pre-auth reachability signal); never mount on the public /share/:token route.
 *  - cadence: 5s while anything is warming, 20s steady; exponential backoff while
 *    unreachable (5→10→20→40→cap 60s), reset on success.
 *  - pauses when the tab is hidden; immediate poll on visibility/focus/pageshow/online.
 */
export function useHealthPoll(enabled: boolean): void {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoff = useRef(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;

    const onOnline = () => { setOnline(true); void tick(); };
    const onOffline = () => setOnline(false);

    function nextDelay(): number {
      const caps = current.caps;
      const warming = !!caps && (
        caps.embeddings.state === "warming" || caps.transcription.state === "warming"
      );
      return pollDelay(backoff.current, warming);
    }

    function schedule() {
      if (timer.current) clearTimeout(timer.current);
      if (!enabled) return;
      timer.current = setTimeout(() => { void tick(); }, nextDelay());
    }

    let inflight = false;
    async function tick() {
      if (!enabled || inflight) return;
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        // Paused while backgrounded — skip the network call but ALWAYS keep the
        // timer chain alive (reschedule) so a missed resume event can never stall
        // polling permanently. A resume event triggers an immediate poll regardless.
        schedule();
        return;
      }
      inflight = true;
      try {
        const r = await getStatus();
        if (!alive.current) return;
        applyPoll(r);
        backoff.current = r.ok ? 0 : backoff.current + 1;
      } finally {
        inflight = false;
        if (alive.current) schedule();
      }
    }

    _refreshNow = () => { void tick(); };
    setOnline(typeof navigator === "undefined" ? true : navigator.onLine);

    const onResume = () => {
      if (typeof document === "undefined" || document.visibilityState === "visible") void tick();
    };

    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    window.addEventListener("focus", onResume);
    window.addEventListener("pageshow", onResume);
    document.addEventListener("visibilitychange", onResume);

    if (enabled) void tick();

    return () => {
      alive.current = false;
      if (timer.current) clearTimeout(timer.current);
      if (_refreshNow) _refreshNow = null;
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("focus", onResume);
      window.removeEventListener("pageshow", onResume);
      document.removeEventListener("visibilitychange", onResume);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);
}
