import { useEffect, useRef, useState } from "react";
import { get, u, getAccessKey } from "../api";

// When the API is down (restarting / failed deploy), the API endpoint can't answer —
// but Caddy stays up and serves the same captured files at /deploy-status/*, gated by
// the access key (HTTP Basic, username "jbrain"). Fetch those as a fallback.
async function fetchViaCaddy(): Promise<{ log: string; status: any } | null> {
  const key = getAccessKey();
  if (!key) return null;
  const headers = { Authorization: "Basic " + btoa("jbrain:" + key) };
  const r = await fetch(u("/deploy-status/update.log"), { headers, cache: "no-store" });
  if (!r.ok) throw new Error(String(r.status));
  const log = await r.text();
  let status: any = null;
  try {
    const s = await fetch(u("/deploy-status/status.json"), { headers, cache: "no-store" });
    if (s.ok) status = await s.json();
  } catch { /* status optional */ }
  return { log, status };
}

// Live update console: polls the captured update.sh / updater output (served by the
// API — and, when it's down, Caddy in phase 2) and streams it into a scrolling view
// with a status indicator and a Copy button. Opened from the System card.
export default function UpdateConsole({ onClose }: { onClose: () => void }) {
  const [log, setLog] = useState("");
  const [state, setState] = useState<string | null>(null);   // running | ok | failed
  const [phase, setPhase] = useState<string>("");            // fetching | building | restarting | health-checking
  const [at, setAt] = useState<string>("");                  // status timestamp (UTC ISO)
  const [reachable, setReachable] = useState(true);          // is the API answering?
  const [copied, setCopied] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);
  const atBottom = useRef(true);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const r: any = await get("/api/system/update-log");   // preferred: via the API
        if (!alive) return;
        setReachable(true);
        setLog(r.log || "");
        setState(r.status?.state ?? null);
        setPhase(r.status?.phase ?? "");
        setAt(r.status?.at ?? "");
      } catch {
        // API unreachable (restarting / failed) → try Caddy's gated static copy.
        try {
          const r = await fetchViaCaddy();
          if (!alive) return;
          if (r) { setReachable(true); setLog(r.log || ""); setState(r.status?.state ?? null); setPhase(r.status?.phase ?? ""); setAt(r.status?.at ?? ""); }
          else setReachable(false);
        } catch {
          if (alive) setReachable(false);
        }
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

  // "Stale" = a finished deploy whose status is more than ~2 min old, i.e. we're
  // looking at the LAST run, not one happening now. Keeps "complete" from masquerading
  // as live (your "it says updating but the console says all good" confusion).
  const ageMs = at ? Date.now() - Date.parse(at) : Infinity;
  const stale = (state === "ok" || state === "failed") && ageMs > 120000;
  const ph = phase ? ` — ${phase}…` : "…";
  const status = !reachable
    ? { dot: "busy", text: "Server offline — restarting (or a failed deploy)…" }
    : state === "running" ? { dot: "busy", text: `Updating${ph}` }
    : state === "ok" ? { dot: "ok", text: stale ? "Last deploy: complete" : "Update complete" }
    : state === "failed" ? { dot: "err", text: stale ? "Last deploy: failed" : "Update failed" }
    : { dot: "info", text: "No deploy recorded yet" };

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
