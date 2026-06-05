import { useEffect, useMemo, useState } from "react";
import { get, getAttachmentLabs, getAttachmentLabSeries, approveLabs, revokeLabs, reanalyzeLabs,
         LabSeries, StagedLab } from "../api";
import LabChart from "./LabChart";

// On a medical note, preview the lab values EXTRACTED from each PDF attachment as a table +
// plot, and approve / revoke / re-analyze them — mirroring the image-analysis panel. Nothing
// reaches the Labs trends until Approve. Shown only for attachments that parsed as labs.

function AttachmentLabs({ id, filename, onChange }: { id: number; filename: string; onChange: () => void }) {
  const [s, setS] = useState<StagedLab | null>(null);
  const [sel, setSel] = useState<string>("");
  const [series, setSeries] = useState<LabSeries | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => getAttachmentLabs(id).then(setS).catch(() => setS(null));
  useEffect(() => { load(); }, [id]);

  // Group staged rows by analyte for the table + the plot picker. Track per-analyte low-confidence
  // counts so the reviewer's attention lands on the rows the parser was least sure of (F3).
  const analytes = useMemo(() => {
    const m = new Map<string, { analyte: string; name: string; unit: string | null; n: number; latest: string; at: string; attime: string; lowconf: number }>();
    for (const r of (s?.results || [])) {
      const g = m.get(r.analyte_key) || { analyte: r.analyte_key, name: r.test_name, unit: r.unit, n: 0, latest: "", at: "", attime: "", lowconf: 0 };
      g.n++; if (r.confidence && r.confidence !== "high") g.lowconf++;   // low OR medium (e.g. photo-derived)
      if ((r.collected_at || "") >= g.at) { g.at = r.collected_at; g.latest = r.value_text; g.attime = r.collected_time || ""; }
      m.set(r.analyte_key, g);
    }
    return [...m.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [s]);

  useEffect(() => { if (analytes.length && !sel) setSel(analytes[0].analyte); }, [analytes, sel]);
  useEffect(() => {
    if (!sel) { setSeries(null); return; }
    getAttachmentLabSeries(id, sel).then(setSeries).catch(() => setSeries(null));
  }, [id, sel, s]);

  if (!s) return null;

  async function act(fn: () => Promise<any>) {
    setBusy(true);
    try { await fn(); await load(); onChange(); } finally { setBusy(false); }
  }

  // Never staged for review. If rows were imported under the old auto-apply path, surface them
  // so they can be Re-analyzed (remove + re-extract) or Removed; otherwise offer a quiet extract.
  if (!s.status) {
    const imported = s.imported || 0;
    return (
      <div className="lab-import">
        <div className="lab-import-head">
          {imported > 0 && <span className="lab-badge ok">in your trends</span>}
          <span className="muted" style={{ fontSize: 12, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{filename}</span>
        </div>
        {imported > 0 && <p className="muted" style={{ fontSize: 13, margin: "2px 0 8px" }}>
          {imported} lab results from this PDF are in your trends (imported before review existed).</p>}
        <div className="lab-import-actions">
          <button className="primary" disabled={busy} onClick={() => act(() => reanalyzeLabs(id))}>
            {imported > 0 ? "Re-analyze (remove & re-extract)" : "Extract lab values"}</button>
          {imported > 0 && <button className="ghost" disabled={busy} onClick={() => act(() => revokeLabs(id))}>Remove</button>}
        </div>
      </div>
    );
  }

  const approved = s.status === "approved";
  const unread = s.status === "error" || s.status === "image_unparsed";
  return (
    <div className="lab-import">
      <div className="lab-import-head">
        <span className={"lab-badge " + (approved ? "ok" : unread ? "err" : "pend")}>
          {approved ? "approved" : unread ? "couldn’t read" : "needs review"}
        </span>
        <span className="muted" style={{ fontSize: 12, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{filename}</span>
      </div>
      {unread ? (
        <p className="muted" style={{ fontSize: 13 }}>
          {s.status === "image_unparsed"
            ? "This looks like a lab image, but I couldn’t verify any values (needs a local OCR engine + AI vision)."
            : "Couldn’t read lab values from this file."}
          <button className="ghost" style={{ marginLeft: 8, fontSize: 12 }} disabled={busy}
                  onClick={() => act(() => reanalyzeLabs(id))}>Re-analyze</button></p>
      ) : (
        <>
          {(s.identity_state === "mismatch" || s.identity_state === "unverified"
            || (s.identity && (s.identity.name || s.identity.dob))) && (
            <p style={{ fontSize: 12, margin: "2px 0 6px",
                        color: s.identity_state === "mismatch" ? "#b00"
                             : s.identity_state === "unverified" ? "#b06f00" : undefined }}>
              {s.identity_state === "mismatch" ? "⚠ Different patient — "
                : s.identity_state === "unverified" ? "⚠ Couldn’t verify patient — " : "Patient: "}
              {s.identity?.name || (s.identity?.dob ? "" : "no patient on document")}
              {s.identity?.dob ? ` (DOB ${s.identity.dob})` : ""}
              {s.identity_state === "mismatch" ? " — does NOT match the configured owner. Check before approving."
                : s.identity_state === "unverified" ? " — no DOB to match against the owner. Confirm before approving." : ""}
            </p>
          )}
          {s.doc_type === "lab_image" && (
            <p style={{ fontSize: 12, margin: "2px 0 6px", color: "#b06f00" }}>
              📷 Read from a photo/scan by AI — check the values against the original, especially any flagged below.
            </p>
          )}
          <p className="muted" style={{ fontSize: 12, margin: "2px 0 8px" }}>
            {s.results.length} results · {analytes.length} analytes
            {s.low_confidence ? ` · ${s.low_confidence} need a look` : ""}
            {approved ? " · in your trends" : " — review before approving"}
          </p>
          {!!(s.skips && s.skips.length) && (
            <details className="lab-table" style={{ marginBottom: 6 }}>
              <summary className="muted" style={{ fontSize: 12 }}>
                {s.skips.length} value{s.skips.length === 1 ? "" : "s"} dropped (not added)
              </summary>
              <table>
                <thead><tr><th>Analyte</th><th>Value</th><th>Date</th><th>Why</th></tr></thead>
                <tbody>
                  {s.skips.map((k, i) => (
                    <tr key={i}><td>{k.analyte}</td><td>{k.value}</td><td>{k.date || "—"}</td>
                      <td className="muted">{k.reason}</td></tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
          {(() => {
            const flagged = (s.results || []).filter((r: any) => r.confidence && r.confidence !== "high");
            return flagged.length > 0 ? (
              <details className="lab-table" style={{ marginBottom: 6 }}>
                <summary className="muted" style={{ fontSize: 12 }}>
                  {flagged.length} value{flagged.length === 1 ? "" : "s"} to double-check
                </summary>
                <table>
                  <thead><tr><th>Analyte</th><th>Value</th><th>Date</th><th>Why</th></tr></thead>
                  <tbody>
                    {flagged.map((r: any, i: number) => (
                      <tr key={i}><td>{r.test_name}</td><td>{r.value_text}</td><td>{r.collected_at || "—"}</td>
                        <td className="muted">{(r.confidence_reasons || []).join("; ") || r.confidence}</td></tr>
                    ))}
                  </tbody>
                </table>
              </details>
            ) : null;
          })()}
          {analytes.length > 0 && (
            <select className="med-dest-select" value={sel} onChange={(e) => setSel(e.target.value)} style={{ marginBottom: 6 }}>
              {analytes.map((a) => <option key={a.analyte} value={a.analyte}>{a.name} ({a.n}){a.lowconf ? " ⚠" : ""}</option>)}
            </select>
          )}
          {series && series.points.length > 0 && <LabChart series={series} height={180} />}
          <details className="lab-table" style={{ marginTop: 6 }}>
            <summary className="muted" style={{ fontSize: 12 }}>Show all {s.results.length} as table</summary>
            <table>
              <thead><tr><th>Analyte</th><th>n</th><th>Latest</th></tr></thead>
              <tbody>
                {analytes.map((a) => (
                  <tr key={a.analyte}><td>{a.name}{a.lowconf ? <span title={`${a.lowconf} value(s) need a look`}> ⚠</span> : ""}</td>
                    <td>{a.n}</td><td>{a.latest}{a.unit ? " " + a.unit : ""} <span className="muted">({a.at}{a.attime ? " " + a.attime : ""})</span></td></tr>
                ))}
              </tbody>
            </table>
          </details>
          <div className="lab-import-actions">
            {approved
              ? <button className="ghost" disabled={busy} onClick={() => act(() => revokeLabs(id))}>Revoke</button>
              : <button className="primary" disabled={busy} onClick={() => act(() => approveLabs(id))}>Approve → add to trends</button>}
            <button className="ghost" disabled={busy} onClick={() => act(() => reanalyzeLabs(id))}>Re-analyze</button>
          </div>
        </>
      )}
    </div>
  );
}

export default function LabImportPanel({ slug, tick }: { slug: string; tick?: number }) {
  const [atts, setAtts] = useState<{ id: number; filename: string; mime: string }[]>([]);
  const [bump, setBump] = useState(0);
  useEffect(() => {
    get<{ id: number; filename: string; mime: string }[]>(`/api/notes/${slug}/attachments`)
      .then((a) => setAtts(a.filter((x) =>
        /pdf|image\//i.test(x.mime) || /\.(pdf|png|jpe?g|gif|webp|heic|tiff?|bmp)$/i.test(x.filename))))
      .catch(() => setAtts([]));
  }, [slug, tick, bump]);
  if (atts.length === 0) return null;
  return (
    <div>
      <h3 style={{ marginTop: 20 }}>Lab import</h3>
      {atts.map((a) => <AttachmentLabs key={a.id} id={a.id} filename={a.filename} onChange={() => setBump((b) => b + 1)} />)}
    </div>
  );
}
