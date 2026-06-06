import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { auditLinks, fixAllLinkLabels, fixLinkLabel, LinkAuditFinding } from "../api";
import { leaf } from "../util";
import { Icon } from "./Icon";

// In-wiki maintenance panel: finds [[Target|Display]] links whose shortened label names a
// DIFFERENT article than the target (e.g. a stale alias left behind by a rename/merge) and
// corrects the label to the target's name. Collapsed + on-demand so a full-KB scan never
// runs on every Wiki open. Mirrors the nightly audit-link-labels workflow.
export default function LinkAuditPanel() {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<"idle" | "scanning" | "done">("idle");
  const [findings, setFindings] = useState<LinkAuditFinding[]>([]);
  const [busy, setBusy] = useState(false);

  async function scan() {
    setState("scanning");
    try {
      const r = await auditLinks();
      setFindings(r.findings);
      if (r.findings.length) setOpen(true);   // surface issues without a click
    } catch { setFindings([]); }
    finally { setState("done"); }
  }
  // Flag on every wiki view: scan (read-only) when the page opens. The nightly
  // audit-link-labels workflow is what actually fixes; viewing only reports.
  useEffect(() => { scan(); }, []);

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && state === "idle") scan();
  }
  async function fixOne(f: LinkAuditFinding) {
    setBusy(true);
    try {
      await fixLinkLabel(f.source_id, f.target, f.display);
      setFindings((xs) => xs.filter((x) => !(x.source_id === f.source_id && x.raw === f.raw)));
    } catch (e: any) { alert(e?.message || "Couldn't fix that link."); }
    finally { setBusy(false); }
  }
  async function fixAll() {
    if (!confirm(`Fix all ${findings.length} link labels? Each change is versioned and undoable.`)) return;
    setBusy(true);
    try { await fixAllLinkLabels(); await scan(); }
    catch (e: any) { alert(e?.message || "Couldn't fix all."); }
    finally { setBusy(false); }
  }

  return (
    <div className="card link-audit">
      <div className="row" style={{ alignItems: "center", cursor: "pointer" }} onClick={toggle}>
        <span className={"chev" + (open ? " open" : "")}><Icon name="chevron" size={14} /></span>
        <strong style={{ marginLeft: 6 }}>Link label audit</strong>
        {state === "done" && findings.length > 0 &&
          <span className="badge tag-delete" style={{ marginLeft: 8 }}>{findings.length}</span>}
        {state === "done" && findings.length === 0 &&
          <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>✓ clean</span>}
        {state === "scanning" && <span className="muted" style={{ marginLeft: 8, fontSize: 12 }}>scanning…</span>}
        <span className="spacer" />
        {open && (
          <button className="ghost" style={{ fontSize: 12 }}
                  onClick={(e) => { e.stopPropagation(); scan(); }} disabled={state === "scanning"}>
            {state === "scanning" ? "Scanning…" : "Rescan"}
          </button>
        )}
      </div>

      {open && (
        <div style={{ marginTop: 8 }}>
          <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
            Links whose shortened label names a different article than the target (e.g. a
            stale alias left by a rename). Fixing sets the label to the target's name —
            versioned and undoable.
          </p>
          {state === "scanning" && <p className="muted">Scanning…</p>}
          {state === "done" && findings.length === 0 && <p className="muted">No mismatched link labels. ✓</p>}
          {findings.length > 0 && (
            <>
              <div className="row" style={{ marginBottom: 6 }}>
                <span className="spacer" />
                <button className="primary" onClick={fixAll} disabled={busy}>Fix all ({findings.length})</button>
              </div>
              {findings.map((f) => (
                <div key={f.source_id + f.raw} className="link-audit-item">
                  <div style={{ fontSize: 13 }}>
                    shows “<strong>{f.display}</strong>” but links to <strong>{leaf(f.target_title)}</strong>
                    {f.resolved_title && <> — “{f.display}” is actually <em>{leaf(f.resolved_title)}</em></>}
                  </div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    in <Link to={`/note/${f.source_slug}`} className="wikilink">{leaf(f.source_title)}</Link>
                    {" · "}fix → <code>{f.fixed}</code>
                  </div>
                  <div className="row" style={{ marginTop: 4, gap: 8 }}>
                    <button className="ghost" style={{ fontSize: 12 }} onClick={() => fixOne(f)} disabled={busy}>Fix</button>
                    <Link className="ghost" style={{ fontSize: 12, padding: "2px 8px" }} to={`/note/${f.source_slug}`}>Open note</Link>
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
