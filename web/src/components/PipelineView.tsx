// Read-only visualisation of an action recipe's step pipeline. Renders from the
// parsed step tree the server returns (no client-side YAML parsing). Variable
// references ({{ ... }}) render as chips; a step's `id` (its output) renders as
// an accent "→ name" chip; for_each is a container with indented child steps.

function Val({ v }: { v: any }) {
  if (typeof v === "string") {
    // split on {{ ... }} so references render as chips
    const parts = v.split(/(\{\{.*?\}\})/g).filter((s) => s !== "");
    return <>{parts.map((p, i) =>
      p.startsWith("{{")
        ? <span key={i} className="ref-chip">{p.slice(2, -2).trim()}</span>
        : <span key={i}>{p}</span>)}</>;
  }
  if (v === null || v === undefined) return <span className="muted">—</span>;
  if (typeof v === "object") return <code style={{ fontSize: 12 }}>{JSON.stringify(v)}</code>;
  return <span>{String(v)}</span>;
}

function StepNode({ step }: { step: any }) {
  if (step && step.for_each !== undefined) {
    return (
      <div className="step-card step-loop">
        <div className="step-head">
          <span className="step-do">for each</span>
          <Val v={step.for_each} />
          {step.id && <span className="out-chip">→ {step.id}</span>}
        </div>
        <div className="step-children">
          {(step.steps || []).map((s: any, i: number) => <StepNode key={i} step={s} />)}
        </div>
      </div>
    );
  }
  const guards: [string, any][] = [];
  if (step?.when != null) guards.push(["when", step.when]);
  if (step?.stop_when != null) guards.push(["stop when", step.stop_when]);
  if (step?.stop_when_empty != null) guards.push(["stop if empty", step.stop_when_empty]);
  return (
    <div className="step-card">
      <div className="step-head">
        <span className="step-do">{step?.do || "—"}</span>
        {step?.id && <span className="out-chip">→ {step.id}</span>}
      </div>
      {guards.map(([k, v], i) => (
        <div key={i} className="guard-pill">{k}: <Val v={v} /></div>
      ))}
      {step?.with && Object.entries(step.with).map(([k, v]) => (
        <div key={k} className="step-input"><span className="step-key">{k}</span><Val v={v} /></div>
      ))}
    </div>
  );
}

export default function PipelineView({ steps }: { steps: any[] }) {
  if (!steps?.length) return <p className="muted" style={{ fontSize: 13 }}>No steps.</p>;
  return <div className="pipeline">{steps.map((s, i) => <StepNode key={i} step={s} />)}</div>;
}
