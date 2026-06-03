import { useEffect, useRef, useState } from "react";
import { get } from "../api";

// Live update console: polls the captured update.sh / updater output (served by the
// API — and, when it's down, Caddy in phase 2) and streams it into a scrolling view
// with a status indicator and a Copy button. Opened from the System card.
export default function UpdateConsole({ onClose }: { onClose: () => void }) {
  const [log, setLog] = useState("");
  const [state, setState] = useState<string | null>(null);   // running | ok | failed
  const [reachable, setReachable] = useState(true);          // is the API answering?
  const [copied, setCopied] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);
  const atBottom = useRef(true);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const r: any = await get("/api/system/update-log");
        if (!alive) return;
        setReachable(true);
        setLog(r.log || "");
        setState(r.status?.state ?? null);
      } catch {
        if (alive) setReachable(false);   // API down — restarting or failed deploy
      }
      if (alive) timer = window.setTimeout(tick, 1200);
    };
    tick();
    return () => { alive = false; if (timer) window.clearTimeout(timer); };
  }, []);

  // Auto-scroll to the newest output, unless the user has scrolled up to read.
  useEffect(() => {
    const el = preRef.current;
    if (el && atBottom.current) el.scrollTop = el.scrollHeight;
  }, [log]);
  const onScroll = () => {
    const el = preRef.current;
    if (el) atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  async function copy() {
    try { await navigator.clipboard.writeText(log); setCopied(true); setTimeout(() => setCopied(false), 1500); }
    catch { /* clipboard blocked — ignore */ }
  }

  const status = !reachable
    ? { dot: "busy", text: "Server offline — restarting (or failed)…" }
    : state === "ok" ? { dot: "ok", text: "Update complete" }
    : state === "failed" ? { dot: "err", text: "Update failed" }
    : state === "running" ? { dot: "busy", text: "Updating…" }
    : { dot: "info", text: "No deploy in progress" };

  return (
    <div className="modal-backdrop compact" onClick={onClose}>
      <div className="modal modal-compact" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="modal-title">Update console</span>
          <span className="spacer" />
          <button className="icon-btn" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="modal-body">
          <pre ref={preRef} className="console-view" onScroll={onScroll}>
            {log || "Waiting for update output… (run ./update.sh on the host, or tap Update server)"}
          </pre>
        </div>
        <div className="modal-foot">
          <span className={"status-dot " + status.dot} />
          <span style={{ fontSize: 13 }}>{status.text}</span>
          <span className="spacer" />
          <button className="ghost" onClick={copy} disabled={!log}>{copied ? "Copied ✓" : "Copy"}</button>
          {state === "ok" && (
            <button className="primary" style={{ padding: "4px 12px" }} onClick={() => window.location.reload()}>Reload</button>
          )}
        </div>
      </div>
    </div>
  );
}
