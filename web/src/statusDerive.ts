// Pure derivation of the status indicator from the health model. No React, no App
// import — so it's unit-testable in isolation (and StatusDot stays a thin view).
import type { Capabilities, HealthModel } from "./health";

export type Level = "ok" | "warn" | "down" | "unknown";
export interface Row { key: string; label: string; level: Level; icon: string }

// Subsystem → line-icon name (keys from Icon.tsx) for the per-capability rows.
// Plain strings, so this stays React-free and the module remains pure/testable.
export const SUBSYS_ICON: Record<keyof Capabilities, string> = {
  llm: "robot",
  local_llm: "robot",
  embeddings: "search",
  transcription: "mic",
  push: "bell",
  geocoder: "pin",
  db: "sql",
};

const SUBSYS_LABEL: Record<keyof Capabilities, Partial<Record<string, string>>> = {
  llm: {
    ready: "AI assistant: ready",
    absent: "AI assistant: no API key configured",
    degraded: "AI assistant: last request failed (key may be revoked or over quota)",
  },
  local_llm: {
    ready: "Local AI: ready",
    pulling: "Local AI: downloading model…",
    warming: "Local AI: loading model…",
    unavailable: "Local AI: server not reachable",
    failed: "Local AI: failed",
  },
  embeddings: {
    ready: "Semantic search: ready",
    warming: "Semantic search: starting up…",
    failed: "Semantic search: failed to load",
    unavailable: "Semantic search: unavailable",
    unknown: "Semantic search: checking…",
  },
  transcription: {
    ready: "Transcription: ready",
    warming: "Transcription: starting up…",
    unavailable: "Transcription: not installed on this server",
    failed: "Transcription: failed to load",
    unknown: "Transcription: checking…",
  },
  push: { ready: "Push notifications: ready", absent: "Push notifications: not configured" },
  geocoder: { ready: "Address lookup: ready", absent: "Address lookup: disabled" },
  db: { ready: "Database: ok", failed: "Database: error" },
};

// A subsystem state → severity for the worst-of rollup and per-row tint.
export function stateLevel(state: string): Level {
  if (state === "ready") return "ok";
  if (state === "failed") return "down";
  if (state === "unknown") return "unknown";
  // warming / unavailable / absent / degraded are all "degraded but server up".
  return "warn";
}

function deriveRows(caps: Capabilities | undefined): Row[] {
  if (!caps) return [];
  return (Object.keys(caps) as (keyof Capabilities)[])
    // local_llm is opt-in: when 'absent' (not configured — the norm) it isn't shown, so
    // it never degrades the rollup for installs that don't run a local model.
    .filter((k) => !(k === "local_llm" && (caps[k] as any)?.state === "absent"))
    .map((k) => {
      const state = (caps[k] as any).state as string;
      const label = SUBSYS_LABEL[k]?.[state] ?? `${k}: ${state}`;
      return { key: k, label, level: stateLevel(state), icon: SUBSYS_ICON[k] };
    });
}

export function deriveStatus(m: HealthModel, brain: string): { level: Level; summary: string; rows: Row[] } {
  if (m.reachability === "browser-offline") {
    return { level: "unknown", summary: "Offline — your device has no network.", rows: [] };
  }
  if (m.reachability === "server-unreachable") {
    return { level: "down", summary: `Can’t reach ${brain} — the server isn’t responding.`, rows: deriveRows(m.caps) };
  }
  if (m.server === "needs-auth") {
    return { level: "warn", summary: "Re-authenticate — your saved key no longer verifies.", rows: deriveRows(m.caps) };
  }
  if (!m.caps) {
    return { level: "unknown", summary: "Checking server status…", rows: [] };
  }
  const rows = deriveRows(m.caps);
  const worst: Level = rows.some((r) => r.level === "down") ? "down"
    : rows.some((r) => r.level === "warn") ? "warn"
    : rows.some((r) => r.level === "unknown") ? "unknown" : "ok";
  const summary = worst === "ok" ? "All systems ready."
    : worst === "down" ? "A subsystem has failed."
    : worst === "warn" ? "Some features are limited." : "Checking…";
  return { level: worst, summary, rows };
}
