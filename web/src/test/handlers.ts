// Default "happy-path" MSW handlers — the baseline server contract for
// component-integration (Tier 2) tests. Each test overrides just the routes it
// cares about via `server.use(...)`. Wildcard (`*/api/...`) prefixes match both
// same-origin and a configured server base. See docs/testing-plan/PLAN.md §4.2.
import { http, HttpResponse } from "msw";

// A fully-ready capability set so feature UIs render their enabled state by default.
// local_llm defaults to 'absent' (the norm — local LLM is opt-in), which the status
// derivation hides so it never degrades the health dot.
export const READY_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: false, local: false } },
  local_llm: { state: "absent" },
  embeddings: { state: "ready" },
  transcription: { state: "ready" },
  push: { state: "ready" },
  geocoder: { state: "ready" },
  db: { state: "ready" },
};

export const handlers = [
  http.get("*/api/system/status", () =>
    HttpResponse.json({ ok: true, version: "test", capabilities: READY_CAPS }),
  ),
  // Local-LLM management defaults: not running (no models), so SystemPage renders the
  // calm "Ollama isn't running" state unless a test overrides this.
  http.get("*/api/system/local-models", () =>
    HttpResponse.json({ running: false, models: [], hardware: { usable_ram_bytes: 0, total_ram_bytes: 0, cpu_only: true, note: "" } }),
  ),
  http.get("*/api/auth/info", () => HttpResponse.json({ brain_name: "Test Brain" })),
  http.get("*/api/auth/verify", () =>
    HttpResponse.json({
      version: "test",
      has_llm: true,
      owner_set: true,
      capabilities: READY_CAPS,
    }),
  ),
];
