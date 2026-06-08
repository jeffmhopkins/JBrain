import { useEffect, useState } from "react";
import { get, put, type LocalModel } from "../api";
import { useCapability } from "../capabilities";

// Pick the LLM model per task. The provider is inferred from the id (grok* → xAI,
// claude* → Anthropic, an Ollama 'name:tag' → local), so this dropdown is the only
// control — selecting writes the matching prompt override (same store the Prompts panel
// uses). Local models (when any are installed) appear in their own optgroup; their value
// is the bare Ollama id (e.g. "qwen2.5:7b"), which the server routes to the local server.
const MODELS: { value: string; label: string; provider: "anthropic" | "xai" | null }[] = [
  { value: "", label: "Provider default", provider: null },
  { value: "claude-opus-4-8", label: "Claude Opus 4.8", provider: "anthropic" },
  { value: "claude-sonnet-4-6", label: "Claude Sonnet 4.6", provider: "anthropic" },
  { value: "claude-haiku-4-5-20251001", label: "Claude Haiku 4.5", provider: "anthropic" },
  { value: "grok-4.3", label: "Grok 4.3 (xAI)", provider: "xai" },
];
const TIERS: { key: string; label: string; sub: string }[] = [
  { key: "agent.model", label: "Chat agent", sub: "the interactive assistant" },
  { key: "models.cheap", label: "Routine jobs", sub: "tags, day summaries, filing" },
  { key: "models.synthesis", label: "KB synthesis", sub: "wiki folding + citations" },
  { key: "models.vision", label: "Image analysis", sub: "attachment vision" },
  { key: "models.default", label: "Fallback", sub: "anything else unspecified" },
];

interface PromptRow { key: string; effective: string; }
const isLocalId = (m: string, local: Set<string>) => local.has(m) || m.includes(":");
const providerOf = (m: string, local: Set<string>) =>
  isLocalId(m, local) ? "local"
    : m.toLowerCase().startsWith("grok") ? "xai"
      : m.toLowerCase().startsWith("claude") ? "anthropic" : null;

export default function ModelPicker({ localModels = [] }: { localModels?: LocalModel[] }) {
  const [vals, setVals] = useState<Record<string, string>>({});
  // The ONLY consumer of the informational per-provider key-presence map. Folds the
  // former private /api/auth/verify re-fetch into the shared health store. Unknown
  // (pre-poll) → assume present so we never show a spurious "missing key" warning.
  const { providers } = useCapability("llm");
  const keys = providers ?? { anthropic: true, xai: true };
  const localNames = new Set(localModels.map((m) => m.name));
  const fitsByName = new Map(localModels.map((m) => [m.name, m.fits]));

  async function load() {
    try {
      const rows = await get<PromptRow[]>("/api/prompts");
      const v: Record<string, string> = {};
      for (const t of TIERS) v[t.key] = rows.find((r) => r.key === t.key)?.effective || "";
      setVals(v);
    } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, []);

  async function set(key: string, value: string) {
    setVals((v) => ({ ...v, [key]: value }));
    try { await put(`/api/prompts/${encodeURIComponent(key)}`, { value }); }
    catch { load(); }
  }

  // Warn once if any chosen model needs a provider key that isn't configured. Local
  // models need no key, so they never trip this.
  const missing = new Set<string>();
  for (const t of TIERS) {
    const p = providerOf(vals[t.key] || "", localNames);
    if ((p === "xai" || p === "anthropic") && !keys[p]) missing.add(p);
  }

  return (
    <div className="card">
      <div className="row"><strong>Model</strong></div>
      <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>
        Pick the LLM per task. The provider follows the model — Grok needs <code>XAI_API_KEY</code> in
        your .env; Claude needs <code>ANTHROPIC_API_KEY</code> (or <code>LLM_API_KEY</code>); local
        models run on Ollama (no key).
      </p>
      {localModels.length > 0 && (
        <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
          Recommended: run routine jobs (tags, summaries) on a local model; keep the chat agent and
          synthesis on the cloud API.
        </p>
      )}
      {[...missing].map((p) => (
        <div key={p} className="muted" style={{ fontSize: 12, color: "var(--danger)", marginBottom: 6 }}>
          A {p === "xai" ? "Grok (xAI)" : "Claude (Anthropic)"} model is selected but its API key
          isn’t configured — that task will fail until you set {p === "xai" ? "XAI_API_KEY" : "ANTHROPIC_API_KEY"}.
        </div>
      ))}
      <div className="model-grid">
        {TIERS.map((t) => {
          const cur = vals[t.key] ?? "";
          const known = MODELS.some((m) => m.value === cur) || localNames.has(cur);
          return (
            <label key={t.key} className="model-row">
              <span className="model-label">{t.label}<span className="muted"> · {t.sub}</span></span>
              <select value={cur} onChange={(e) => set(t.key, e.target.value)}>
                {!known && <option value={cur}>{cur} (custom)</option>}
                <optgroup label="Cloud">
                  {MODELS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
                </optgroup>
                {localModels.length > 0 && (
                  <optgroup label="Local (Ollama)">
                    {localModels.map((m) => (
                      <option key={m.name} value={m.name} disabled={m.fits === false}>
                        {m.name}{m.fits === false ? " — won’t fit" : ""}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
              {isLocalId(cur, localNames) && fitsByName.get(cur) === false && (
                <span className="muted" style={{ fontSize: 12, color: "var(--danger)" }}>
                  This local model won’t fit in available memory — pick a smaller one.
                </span>
              )}
            </label>
          );
        })}
      </div>
    </div>
  );
}
