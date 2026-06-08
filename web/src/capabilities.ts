// Pre-flight capability gating: a single source of "is feature X usable, and if not,
// why" so every entry point disables-and-explains consistently instead of letting the
// call fail. Reads the health store (declared caps + observed overlay).
import { useHealth, type CapState } from "./health";

export type CapId = "llm" | "embeddings" | "transcription" | "push" | "geocoder";

// Single source of truth for the "why it's unavailable" copy, keyed by capability +
// the non-ready state. (ready → no copy.) The status panel and gates both read this.
export const CAP_COPY: Record<CapId, Partial<Record<CapState, string>>> = {
  llm: {
    absent: "AI features need an API key for the active provider — set the key for your LLM_PROVIDER on the server.",
    degraded: "AI key present but the last request failed — it may be revoked or over quota.",
    unknown: "Checking AI availability…",
  },
  embeddings: {
    warming: "Semantic search is still loading its local model — try again in a few seconds.",
    failed: "Semantic search is unavailable (the embedding model failed to load).",
    unavailable: "Semantic search is unavailable.",
    unknown: "Checking semantic search…",
  },
  transcription: {
    warming: "Transcription is loading its model — try again shortly.",
    unavailable: "Audio/video transcription isn’t installed on this server.",
    failed: "Transcription failed to load.",
    unknown: "Checking transcription…",
  },
  geocoder: { absent: "Address lookup is disabled (no geocoder configured)." },
  push: { absent: "Push notifications aren’t configured on this server." },
};

export interface CapStatus {
  ready: boolean;
  state: CapState;
  reason: string;            // "" when ready
  providers?: { anthropic: boolean; xai: boolean };   // llm only, informational
}

// Selects primitive slices (reachability, the cap's state string) so the common path
// doesn't re-render unrelated consumers. The `providers` slice returns the server's
// object, which is rebuilt each poll — fine (no infinite loop: getSnapshot returns the
// cached `current`, stable within a render), it just re-renders llm consumers per poll.
export function useCapability(id: CapId): CapStatus {
  const reachability = useHealth((m) => m.reachability);
  const state = useHealth((m) => (m.caps?.[id]?.state ?? "unknown")) as CapState;
  const providers = useHealth((m) => (id === "llm" ? m.caps?.llm?.providers : undefined));

  // Everything runs on the server, so if the server isn't reachable, nothing is usable —
  // disable-and-explain rather than letting the action fail.
  if (reachability === "browser-offline") {
    return { ready: false, state, reason: "You’re offline — changes can’t be saved right now.", providers };
  }
  if (reachability === "server-unreachable") {
    return { ready: false, state, reason: "Server unreachable — retrying…", providers };
  }
  const ready = state === "ready";
  const reason = ready ? "" : (CAP_COPY[id]?.[state] ?? "This feature is currently unavailable.");
  return { ready, state, reason, providers };
}
