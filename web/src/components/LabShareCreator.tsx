import { useMemo, useState } from "react";
import { LabAnalyte, createLabShare } from "../api";
import Modal from "./Modal";
import ShareOptions, { ShareCommonOptions } from "./ShareOptions";

// Owner authoring UI (a Modal): pick exactly which lab results to share, optionally allow a
// scoped AI chat, and mint a bind-locked, expiring link. Only the ticked analytes are ever
// exposed — no names, no notes. Mirrors the app's share-modal patterns (ResearchLinks / NotePage).

const OUT = new Set(["high", "low", "abnormal"]);
function pip(status: string) {
  const c = OUT.has(status) ? "var(--danger)" : status === "normal" ? "var(--ok)" : "var(--text-dim)";
  const ch = status === "high" ? "▲" : status === "low" ? "▼" : status === "normal" ? "●" : "○";
  return <span style={{ color: c, fontSize: 10 }} title={status}>{ch}</span>;
}

export default function LabShareCreator({ analytes }: { analytes: LabAnalyte[] }) {
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [q, setQ] = useState("");
  const [from, setFrom] = useState("");   // optional date window — empty = full history
  const [to, setTo] = useState("");
  const [allowChat, setAllowChat] = useState(true);
  // PHI defaults: locked to one device, finite expiry (no "never"), modest reply caps.
  const [opts, setOpts] = useState<ShareCommonOptions>(
    { bind: true, single_use: false, ttl_days: 14, max_turns: 30, max_total_replies: 120 });
  const [busy, setBusy] = useState(false);
  const [url, setUrl] = useState("");
  const [copied, setCopied] = useState(false);
  const [err, setErr] = useState("");

  const filtered = useMemo(() => {
    const f = q.trim().toLowerCase();
    return analytes.filter((a) => !f || a.test_name.toLowerCase().includes(f) || a.analyte.includes(f));
  }, [analytes, q]);
  const allFilteredSelected = filtered.length > 0 && filtered.every((a) => sel.has(a.analyte));

  function toggle(a: string) {
    setSel((s) => { const n = new Set(s); n.has(a) ? n.delete(a) : n.add(a); return n; });
  }
  function selectAllFiltered() {
    setSel((s) => { const n = new Set(s); filtered.forEach((a) => n.add(a.analyte)); return n; });
  }
  function clearAll() { setSel(new Set()); }

  function close() { setOpen(false); setUrl(""); setErr(""); setQ(""); }

  async function create() {
    if (!sel.size) { setErr("Pick at least one result to share."); return; }
    setBusy(true); setErr("");
    try {
      const r = await createLabShare([...sel], { allow_chat: allowChat,
        window_from: from || null, window_to: to || null,
        bind: opts.bind, single_use: opts.single_use, ttl_days: opts.ttl_days,
        max_turns: opts.max_turns, max_total_replies: opts.max_total_replies });
      setUrl(r.url || "");
    } catch (e: any) { setErr(e?.message || "Couldn't create the link."); }
    finally { setBusy(false); }
  }
  function copy() {
    navigator.clipboard?.writeText(url);
    setCopied(true); setTimeout(() => setCopied(false), 1200);
  }
  function newLink() { setUrl(""); setSel(new Set()); setErr(""); }

  if (!analytes.length) return null;

  const minted = !!url;
  const footer = minted ? (
    <>
      <button className="ghost" onClick={newLink}>New link</button>
      <span className="spacer" />
      <button className="primary" onClick={copy}>{copied ? "Copied ✓" : "Copy link"}</button>
    </>
  ) : (
    <>
      <button className="ghost" onClick={close}>Cancel</button>
      <span className="spacer" />
      <span className="muted" style={{ fontSize: 12 }}>{sel.size} selected</span>
      <button className="primary" disabled={busy || !sel.size} onClick={create}>
        {busy ? "…" : "Create link"}</button>
    </>
  );

  return (
    <>
      <button className="chip" onClick={() => setOpen(true)}>Share results…</button>
      {open && (
        <Modal title="Share lab results" onClose={close} footer={footer}>
          {minted ? (
            <div className="share-stack">
              <div className="share-minted-head">
                <strong>Link created</strong>
                <span className="badge">{sel.size} result{sel.size === 1 ? "" : "s"}</span>
                {allowChat && <span className="badge">AI chat</span>}
              </div>
              <input className="share-url" readOnly value={url} onFocus={(e) => e.currentTarget.select()} />
              <p className="share-hint">
                Locked to the first device that opens it · expires in {opts.ttl_days} days · only the
                {" "}{sel.size} result{sel.size === 1 ? "" : "s"} above are visible (no names, no notes).
              </p>
            </div>
          ) : (
            <div className="share-stack">
              <p className="share-hint">
                Pick exactly what to share. Only the ticked results are ever visible — no names, no
                notes. The link locks to the first device that opens it and expires.
              </p>

              <div className="adv-section" style={{ margin: "4px 4px 6px" }}>
                Results to share ({sel.size}/{analytes.length})
                <button className="ghost" style={{ fontSize: 11, padding: "2px 8px", marginLeft: 8 }}
                        onClick={allFilteredSelected ? clearAll : selectAllFiltered}>
                  {allFilteredSelected ? "Clear" : "Select all"}</button>
              </div>
              <input placeholder="Find a result…" value={q} onChange={(e) => setQ(e.target.value)}
                     style={{ marginBottom: 8 }} />
              <div className="lab-share-list">
                {filtered.map((a) => (
                  <label key={a.analyte} className={"lab-share-opt" + (sel.has(a.analyte) ? " sel" : "")}>
                    <input type="checkbox" className="share-check" checked={sel.has(a.analyte)}
                           onChange={() => toggle(a.analyte)} />
                    <span className="lab-pick-name" style={{ fontWeight: 600 }}>{a.test_name}</span>
                    <span className="lab-share-opt-val muted">
                      {pip(a.last_status)} {a.last_value}{a.unit ? " " + a.unit : ""}</span>
                  </label>
                ))}
                {filtered.length === 0 && <div className="muted" style={{ padding: 12, fontSize: 13 }}>No results match.</div>}
              </div>

              <div className="adv-section">What they can do</div>
              <label className="share-row share-toggle">
                <span className="share-row-label">Let them ask an AI about these results</span>
                <input type="checkbox" className="share-check" checked={allowChat}
                       onChange={(e) => setAllowChat(e.target.checked)} />
                <span className="share-help share-toggle-help">
                  Scoped to the shared results only — it can’t see anything else.</span>
              </label>

              <div className="adv-section">Date range (optional)</div>
              <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                <label className="row" style={{ gap: 6 }}>From
                  <input type="date" value={from} max={to || undefined}
                         onChange={(e) => setFrom(e.target.value)} /></label>
                <label className="row" style={{ gap: 6 }}>To
                  <input type="date" value={to} min={from || undefined}
                         onChange={(e) => setTo(e.target.value)} /></label>
                {(from || to) && <button className="ghost" style={{ fontSize: 12 }}
                  onClick={() => { setFrom(""); setTo(""); }}>All time</button>}
              </div>

              <div className="adv-section">Link settings</div>
              <ShareOptions value={opts} onChange={(p) => setOpts((o) => ({ ...o, ...p }))}
                            show={{ singleUse: true, caps: allowChat, ttlMin: 1 }} disabled={busy} />
              {err && <p className="share-error">{err}</p>}
            </div>
          )}
        </Modal>
      )}
    </>
  );
}
