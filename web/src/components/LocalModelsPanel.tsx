import { useState } from "react";
import { deleteLocalModel, pullLocalModel, type LocalModel, type LocalModelsResponse, type PullEvent } from "../api";

// "Local models (Ollama)" settings card: list installed local models, pull a curated
// one with live progress, and remove one — all hardware-aware (the server tells us what
// fits). Assigning a model to a task tier happens in ModelPicker; this card just manages
// what's installed. Parent (SystemPage) owns the installed-models fetch and passes a
// refresh() so a pull/delete updates both this card and ModelPicker's dropdowns.

// Curated, vetted models offered for one-click pull. Sizes are approximate (for the
// client-side fit hint); the server is authoritative for installed models. The RAM note
// is the resident footprint (≈ size × 1.3) — the larger ones need a high-memory box
// (e.g. a 128 GB Strix Halo); on a 32 GB box the server marks them "won't fit".
const CURATED: { name: string; label: string; approxBytes: number; role: string }[] = [
  // Small — fine on a CPU / 32 GB box (the cheap-tier sweet spot).
  { name: "qwen2.5:7b", label: "Qwen2.5 7B", approxBytes: 4.7e9, role: "Recommended for routine jobs (cheap tier) · ~6 GB RAM" },
  { name: "llama3.1:8b", label: "Llama 3.1 8B", approxBytes: 4.9e9, role: "Alternative routine model · ~6 GB RAM" },
  { name: "llava:7b", label: "LLaVA 7B", approxBytes: 4.7e9, role: "Vision (image analysis) · ~6 GB RAM" },
  // Large — need a high-memory box (e.g. 128 GB unified). Disabled where they won't fit.
  { name: "qwen2.5:14b", label: "Qwen2.5 14B", approxBytes: 9e9, role: "Stronger agent · needs ~12 GB RAM" },
  { name: "qwen2.5:32b", label: "Qwen2.5 32B", approxBytes: 2.0e10, role: "High-quality agent · needs ~26 GB RAM" },
  { name: "llama3.3:70b", label: "Llama 3.3 70B", approxBytes: 4.3e10, role: "Top quality · needs ~56 GB RAM (e.g. 128 GB unified)" },
  { name: "gpt-oss:120b", label: "GPT-OSS 120B (MoE)", approxBytes: 6.5e10, role: "Largest · needs ~85 GB RAM (128 GB box)" },
];

const fmtBytes = (n: number) => {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
};

const dotClass = (state: string) =>
  state === "ready" ? "ok"
    : state === "failed" || state === "unavailable" ? "err"
      : state === "pulling" || state === "warming" ? "busy" : "";

export default function LocalModelsPanel({ data, onRefresh }: {
  data: LocalModelsResponse | null;
  onRefresh: () => void;
}) {
  const [pulling, setPulling] = useState<string | null>(null);
  const [pct, setPct] = useState(0);
  const [phase, setPhase] = useState("");
  const [err, setErr] = useState("");
  const handleRef = useState<{ abort: () => void } | null>(null);

  if (!data) return null;

  if (!data.running) {
    return (
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Local models (Ollama)</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Ollama isn’t running on this server. Local models are optional — the cloud API
          still works. Enable the <code>localllm</code> compose profile (or install Ollama
          on the host) to use them. See the README “Local LLM” section.
        </p>
      </div>
    );
  }

  const usable = data.hardware.usable_ram_bytes;
  const installed = new Set(data.models.map((m) => m.name));
  const curatedToOffer = CURATED.filter((c) => !installed.has(c.name));

  function curatedFit(approxBytes: number): { fits: boolean; warn: string | null } {
    const est = approxBytes * 1.3;
    const fits = usable === 0 || est <= usable;
    // "Slow" only matters on CPU — a GPU box (Strix Halo) runs big models fine.
    const warn = fits && est > 9e9 && data!.hardware.cpu_only ? "Large — slow on CPU" : null;
    return { fits, warn };
  }

  async function doPull(name: string, slow: boolean) {
    if (slow && !window.confirm("This model will run slowly on this hardware. Pull it anyway?")) return;
    setErr(""); setPct(0); setPhase("starting…"); setPulling(name);
    const h = pullLocalModel(name, (e: PullEvent) => {
      if (e.type === "progress") { setPhase(e.status || "downloading"); if (e.total) setPct(Math.round((e.completed / e.total) * 100)); }
      else if (e.type === "status") setPhase(e.status);
      else if (e.type === "error") setErr(e.message);
    });
    handleRef[1](h);
    try { await h.done; } catch { /* aborted */ }
    setPulling(null); setPhase(""); setPct(0);
    onRefresh();
  }

  async function doDelete(name: string) {
    if (!window.confirm(`Remove the local model “${name}”?`)) return;
    try { await deleteLocalModel(name); } catch { /* surfaced by refresh */ }
    onRefresh();
  }

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Local models (Ollama)</h3>
      <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>
        {data.hardware.note} ~{fmtBytes(usable)} usable for a model. Assign one to a task in
        the Model card above — the recommended setup is a 7–8B model for routine jobs.
      </p>

      {data.models.length > 0 ? (
        <ul className="local-model-list" style={{ listStyle: "none", padding: 0, margin: "8px 0" }}>
          {data.models.map((m: LocalModel) => (
            <li key={m.name} className="row" style={{ gap: 8, alignItems: "center", justifyContent: "space-between" }}>
              <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span className={"status-dot " + dotClass(m.state)} />
                <strong>{m.name}</strong>
                <span className="muted" style={{ fontSize: 12 }}>{fmtBytes(m.size_bytes)}</span>
                {m.warn && <span className="muted" style={{ fontSize: 12, color: "var(--warn, #b80)" }}>· {m.warn}</span>}
              </span>
              <button className="ghost" style={{ padding: "2px 10px" }} onClick={() => doDelete(m.name)}>Remove</button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted" style={{ fontSize: 13 }}>No local models installed yet — pull one below.</p>
      )}

      {curatedToOffer.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>Pull a model</div>
          {curatedToOffer.map((c) => {
            const { fits, warn } = curatedFit(c.approxBytes);
            const busy = pulling === c.name;
            return (
              <div key={c.name} className="row" style={{ gap: 8, alignItems: "center", justifyContent: "space-between" }}>
                <span>
                  <strong>{c.label}</strong> <span className="muted" style={{ fontSize: 12 }}>~{fmtBytes(c.approxBytes)} · {c.role}</span>
                  {!fits && <span className="muted" style={{ fontSize: 12, color: "var(--danger)" }}> · won’t fit in {fmtBytes(usable)}</span>}
                  {fits && warn && <span className="muted" style={{ fontSize: 12, color: "var(--warn, #b80)" }}> · {warn}</span>}
                </span>
                <button className="ghost" style={{ padding: "2px 10px" }} disabled={!fits || !!pulling}
                        onClick={() => doPull(c.name, !!warn)}>
                  {busy ? "Pulling…" : "Pull"}
                </button>
              </div>
            );
          })}
        </div>
      )}

      {pulling && (
        <div style={{ marginTop: 10 }}>
          <div className="muted" style={{ fontSize: 12 }}>Pulling {pulling} — {phase} {pct ? `${pct}%` : ""}</div>
          <progress value={pct} max={100} style={{ width: "100%" }} />
          <button className="ghost" style={{ padding: "2px 10px", marginTop: 4 }}
                  onClick={() => handleRef[0]?.abort()}>Cancel</button>
        </div>
      )}
      {err && <p className="muted" style={{ fontSize: 13, color: "var(--danger)", marginTop: 8 }}>{err}</p>}
    </div>
  );
}
