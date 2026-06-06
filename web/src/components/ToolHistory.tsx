import { useEffect, useState } from "react";
import type { NavigateFunction } from "react-router-dom";
import { getMessageSteps, MessageStep } from "../api";
import { toolLabelDone } from "../toolLabels";
import { eventSummary, noteRefs, prettyArgs } from "../toolHistoryParse";
import { CitationLink } from "./CitationLink";
import { Icon } from "./Icon";

// "How I answered this" — the per-reply tool-call history. A pill under an assistant bubble
// (also opened by a left-swipe on the bubble) expands a timeline of the tools the AI ran:
// what it searched/read, what SQL it ran, what it staged/applied — with the notes & kb it
// touched as clickable chips. Results are shown as PLAIN, escaped text (they're untrusted
// note content), so nothing in a note body can inject markup into the UI.

function StepRow({ step, navigate }: { step: MessageStep; navigate: NavigateFunction }) {
  const [open, setOpen] = useState(false);
  const refs = noteRefs(step.result_text);
  const args = prettyArgs(step.args_json);
  const evt = eventSummary(step.event_json);
  return (
    <li className={`th-step${step.is_error ? " th-step-err" : ""}`}>
      <button className="th-step-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <Icon name="chevron" size={14} className={open ? "th-caret th-caret-open" : "th-caret"} />
        <span className="th-step-label">{toolLabelDone(step.tool_name)}</span>
        <code className="th-tool">{step.tool_name}</code>
        {step.is_error ? <span className="th-badge th-badge-err">failed</span> : null}
      </button>
      {evt && <div className="th-event">↳ {evt}</div>}
      {refs.length > 0 && (
        <div className="th-refs">
          {refs.map((r) => (
            <span key={r.slug} className="th-chip">
              <CitationLink href={`/note/${r.slug}`} navigate={navigate}>{r.label}</CitationLink>
            </span>
          ))}
        </div>
      )}
      {open && (
        <div className="th-raw">
          {args && <><div className="th-raw-h">Input</div><pre className="th-pre">{args}</pre></>}
          <div className="th-raw-h">Result</div>
          <pre className="th-pre">{step.result_text || "(no output)"}</pre>
        </div>
      )}
    </li>
  );
}

export default function ToolHistory(
  { messageId, count, open, onToggle, navigate }:
  { messageId: number; count: number; open: boolean; onToggle: () => void; navigate: NavigateFunction },
) {
  const [steps, setSteps] = useState<MessageStep[] | null>(null);
  const [error, setError] = useState(false);

  // Lazy-load the full log only when the panel is first opened (the pill already knows the count).
  useEffect(() => {
    if (!open || steps !== null) return;
    let alive = true;
    getMessageSteps(messageId)
      .then((s) => { if (alive) setSteps(s); })
      .catch(() => { if (alive) setError(true); });
    return () => { alive = false; };
  }, [open, messageId, steps]);

  const label = `${count} ${count === 1 ? "step" : "steps"}`;
  return (
    <div className="tool-history">
      <button className="th-pill" onClick={onToggle} aria-expanded={open}
              title={open ? "Hide how this was answered" : "How this was answered"}>
        <Icon name="robot" size={13} />
        <span>{label}</span>
        <Icon name="chevron" size={13} className={open ? "th-caret th-caret-open" : "th-caret"} />
      </button>
      {open && (
        <div className="th-panel">
          {error ? <div className="th-msg muted">Couldn’t load the history.</div>
            : steps === null ? <div className="th-msg muted">Loading…</div>
            : steps.length === 0 ? <div className="th-msg muted">No tool calls recorded.</div>
            : <ol className="th-steps">{steps.map((s) => <StepRow key={s.step_index} step={s} navigate={navigate} />)}</ol>}
        </div>
      )}
    </div>
  );
}
