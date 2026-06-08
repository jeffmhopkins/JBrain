import { useEffect, useRef, useState } from "react";
import { useAuth } from "../App";
import { refreshNow, useHealth } from "../health";
import { deriveStatus } from "../statusDerive";

export default function StatusDot() {
  const brainName = useAuth()?.brainName || "the server";
  const model = useHealth((m) => m);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const { level, summary, rows } = deriveStatus(model, brainName);
  const title = `Server status: ${summary}`;

  return (
    <div className="status-wrap" ref={ref}>
      <button
        className="status-dot-btn"
        title={title}
        aria-label={title}
        aria-expanded={open}
        onClick={() => { setOpen((o) => !o); refreshNow(); }}
      >
        <span className={`status-dot ${level}`} />
      </button>
      {open && (
        <div className="status-panel" role="dialog" aria-label="Server status">
          <div className="status-panel-summary">{summary}</div>
          {rows.length > 0 && (
            <ul className="status-rows">
              {rows.map((r) => (
                <li key={r.key}>
                  <span className={`status-dot ${r.level}`} />
                  <span>{r.label}</span>
                </li>
              ))}
            </ul>
          )}
          <button className="ghost status-refresh" onClick={() => refreshNow()}>Refresh</button>
        </div>
      )}
    </div>
  );
}
