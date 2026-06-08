import { useEffect, useState } from "react";
import { get, put } from "../api";
import { useCapability } from "../capabilities";

// Pick the LLM model per task. The provider is inferred from the id (grok* → xAI,
// claude* → Anthropic), so this dropdown is the only control — selecting writes the
// matching prompt override (same store the Prompts panel uses).
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
const providerOf = (m: string) =>
  m.toLowerCase().startsWith("grok") ? "xai" : m.toLowerCase().startsWith("claude") ? "anthropic" : null;

export default function ModelPicker() {
  const [vals, setVals] = useState<Record<string, string>>({});
  // The ONLY consumer of the informational per-provider key-presence map. Folds the
  // former private /api/auth/verify re-fetch into the shared health store. Unknown
  // (pre-poll) → assume present so we never show a spurious "missing key" warning.
  const { providers } = useCapability("llm");
  const keys = providers ?? { anthropic: true, xai: true };

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

  // Warn once if any chosen model needs a provider key that isn't configured.
  const missing = new Set<string>();
  for (const t of TIERS) {
    const p = providerOf(vals[t.key] || "");
    if (p && !keys[p]) missing.add(p);
  }

  return (
    <div className="card">
      <div className="row"><strong>Model</strong></div>
      <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>
        Pick the LLM per task. The provider follows the model — Grok needs <code>XAI_API_KEY</code> in
        your .env; Claude needs <code>ANTHROPIC_API_KEY</code> (or <code>LLM_API_KEY</code>).
      </p>
      {[...missing].map((p) => (
        <div key={p} className="muted" style={{ fontSize: 12, color: "var(--danger)", marginBottom: 6 }}>
          A {p === "xai" ? "Grok (xAI)" : "Claude (Anthropic)"} model is selected but its API key
          isn’t configured — that task will fail until you set {p === "xai" ? "XAI_API_KEY" : "ANTHROPIC_API_KEY"}.
        </div>
      ))}
      <div className="model-grid">
        {TIERS.map((t) => {
          const cur = vals[t.key] ?? "";
          const known = MODELS.some((m) => m.value === cur);
          return (
            <label key={t.key} className="model-row">
              <span className="model-label">{t.label}<span className="muted"> · {t.sub}</span></span>
              <select value={cur} onChange={(e) => set(t.key, e.target.value)}>
                {!known && <option value={cur}>{cur} (custom)</option>}
                {MODELS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </label>
          );
        })}
      </div>
    </div>
  );
}
