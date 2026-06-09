import { useEffect, useRef, useState } from "react";
import { useAuth } from "../App";
import { resetAi } from "../api";
import { refreshNow, useHealth } from "../health";
import { deriveStatus } from "../statusDerive";
import { Icon } from "./Icon";

export default function StatusDot() {
  const brainName = useAuth()?.brainName || "the server";
  const model = useHealth((m) => m);
  const [open, setOpen] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [resetting, setResetting] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Recovery for a wedged AI layer (e.g. a chat/research turn that stopped responding after an
  // "Edit with AI" panel was closed mid-stream): drop the cached provider clients + cancel any
  // orphaned rebuild runs server-side, then re-poll health. No data loss.
  async function doResetAi() {
    if (resetting) return;
    setResetting(true);
    try { await resetAi(); } catch { /* best-effort recovery */ }
    finally { setResetting(false); refreshNow(); }
  }

  useEffect(() => {
    if (!open) { setShowAll(false); return; }   // always reopen in the calm, collapsed state
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const { level, summary, rows } = deriveStatus(model, brainName);
  const title = `Server status: ${summary}`;

  // Whole-panel conditions (offline / unreachable / needs-auth / checking): the
  // per-subsystem rows are absent or stale, so the verdict line stands alone.
  const wholePanel = rows.length === 0 || model.reachability !== "online" || model.server === "needs-auth";
  const problems = wholePanel ? [] : rows.filter((r) => r.level !== "ok");
  const healthy = wholePanel ? [] : rows.filter((r) => r.level === "ok");
  const allOk = !wholePanel && problems.length === 0;
  const sub = allOk
    ? `${rows.length}/${rows.length} operational`
    : problems.length === 1 ? "1 system needs attention" : `${problems.length} systems need attention`;

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
          {/* Verdict: the headline the panel leads with. The dot reinforces the
              level; the sentence carries the meaning (never colour-alone). */}
          <div className={`status-verdict ${level}`} aria-live="polite">
            <span className={`status-dot ${level}`} />
            <div className="status-verdict-text">
              <div className="status-verdict-title">{summary}</div>
              {!wholePanel && <div className="status-verdict-sub">{sub}</div>}
            </div>
          </div>

          {/* Problems float to the top as full rows — the reason stays visible. */}
          {problems.length > 0 && (
            <ul className="status-problems">
              {problems.map((r) => (
                <li key={r.key} className={`status-problem ${r.level}`}>
                  <Icon name={r.icon || "dots"} size={16} />
                  <span>{r.label}</span>
                </li>
              ))}
            </ul>
          )}

          {/* Only the healthy remainder collapses — never a reason. All-green →
              "Show all"; partial failure → "N others operational". */}
          {healthy.length > 0 && (
            <>
              <button
                className="status-showall"
                aria-expanded={showAll}
                aria-controls="status-healthy-rows"
                onClick={() => setShowAll((s) => !s)}
              >
                <span className="status-pips" aria-hidden="true">
                  {healthy.map((r) => <span key={r.key} className={`status-pip ${r.level}`} />)}
                </span>
                <span>{allOk ? "Show all" : healthy.length === 1 ? "1 other operational" : `${healthy.length} others operational`}</span>
                <Icon name="chevron" size={14} />
              </button>
              {showAll && (
                <ul className="status-rows" id="status-healthy-rows">
                  {healthy.map((r) => (
                    <li key={r.key}>
                      <Icon name={r.icon || "dots"} size={15} className="status-row-icon" />
                      <span>{r.label}</span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}

          <div className="status-actions">
            <button className="ghost status-refresh" onClick={() => refreshNow()}>Refresh</button>
            <button className="ghost status-reset" onClick={doResetAi} disabled={resetting}
                    title="Drop cached AI connections and cancel stuck rebuilds — use if the AI stops responding">
              {resetting ? "Resetting…" : "Reset AI"}</button>
          </div>
        </div>
      )}
    </div>
  );
}
