import { useState } from "react";
import { LabAnalyte, createLabShare } from "../api";

// Owner authoring UI: pick which lab results to share, whether to allow the scoped AI chat, and
// mint a bind-locked, expiring share link. Only the ticked analytes are ever exposed.
export default function LabShareCreator({ analytes }: { analytes: LabAnalyte[] }) {
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [allowChat, setAllowChat] = useState(true);
  const [busy, setBusy] = useState(false);
  const [url, setUrl] = useState("");
  const [err, setErr] = useState("");

  function toggle(a: string) {
    setSel((s) => { const n = new Set(s); n.has(a) ? n.delete(a) : n.add(a); return n; });
  }
  async function create() {
    if (!sel.size) { setErr("Pick at least one result to share."); return; }
    setBusy(true); setErr(""); setUrl("");
    try {
      const r = await createLabShare([...sel], { allow_chat: allowChat });
      setUrl(r.url || "");
    } catch (e: any) { setErr(e?.message || "Couldn't create the link."); }
    finally { setBusy(false); }
  }

  if (!analytes.length) return null;
  return (
    <div className="lab-share-creator" style={{ marginTop: 24, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
      <button className="chip" onClick={() => setOpen((o) => !o)}>
        {open ? "Close" : "Share results…"}
      </button>
      {open && (
        <div style={{ marginTop: 10 }}>
          <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
            Pick exactly what to share. The link is locked to the first device that opens it and
            expires in 14 days. Only ticked results are ever visible — nothing else, no names or notes.
          </p>
          <div className="lab-picker" style={{ maxHeight: 220, overflowY: "auto" }}>
            {analytes.map((a) => (
              <label key={a.analyte} className="lab-pick" style={{ cursor: "pointer" }}>
                <input type="checkbox" checked={sel.has(a.analyte)} onChange={() => toggle(a.analyte)} />
                <span className="lab-pick-name">{a.test_name}</span>
                <span className="muted" style={{ fontSize: 11 }}>{a.last_value}{a.unit ? " " + a.unit : ""}</span>
              </label>
            ))}
          </div>
          <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, margin: "8px 0" }}>
            <input type="checkbox" checked={allowChat} onChange={(e) => setAllowChat(e.target.checked)} />
            Let them ask an AI questions about these results (scoped to the shared results only)
          </label>
          <div className="row" style={{ gap: 8 }}>
            <button className="primary" disabled={busy || !sel.size} onClick={create}>
              {busy ? "…" : `Create link (${sel.size})`}</button>
          </div>
          {err && <p style={{ color: "var(--danger)", fontSize: 13 }}>{err}</p>}
          {url && (
            <div style={{ marginTop: 10 }}>
              <p className="muted" style={{ fontSize: 12, marginBottom: 4 }}>Share this link:</p>
              <div className="row" style={{ gap: 8 }}>
                <input readOnly value={url} onFocus={(e) => e.currentTarget.select()} style={{ flex: 1 }} />
                <button className="chip" onClick={() => navigator.clipboard?.writeText(url)}>Copy</button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
