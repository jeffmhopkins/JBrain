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

  // Group staged rows by analyte for the table + the plot picker.
  const analytes = useMemo(() => {
    const m = new Map<string, { analyte: string; name: string; unit: string | null; n: number; latest: string; at: string }>();
    for (const r of (s?.results || [])) {
      const g = m.get(r.analyte_key) || { analyte: r.analyte_key, name: r.test_name, unit: r.unit, n: 0, latest: "", at: "" };
      g.n++; if ((r.collected_at || "") >= g.at) { g.at = r.collected_at; g.latest = r.value_text; }
      m.set(r.analyte_key, g);
    }
    return [...m.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [s]);

  useEffect(() => { if (analytes.length && !sel) setSel(analytes[0].analyte); }, [analytes, sel]);
  useEffect(() => {
    if (!sel) { setSeries(null); return; }
    getAttachmentLabSeries(id, sel).then(setSeries).catch(() => setSeries(null));
  }, [id, sel, s]);

  if (!s || !s.status) return null;

  async function act(fn: () => Promise<any>) {
    setBusy(true);
    try { await fn(); await load(); onChange(); } finally { setBusy(false); }
  }

  const approved = s.status === "approved";
  return (
    <div className="lab-import">
      <div className="lab-import-head">
        <span className={"lab-badge " + (approved ? "ok" : s.status === "error" ? "err" : "pend")}>
          {approved ? "approved" : s.status === "error" ? "couldn’t parse" : "needs review"}
        </span>
        <span className="muted" style={{ fontSize: 12, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{filename}</span>
      </div>
      {s.status === "error" ? (
        <p className="muted" style={{ fontSize: 13 }}>Couldn’t read lab values from this PDF.
          <button className="ghost" style={{ marginLeft: 8, fontSize: 12 }} disabled={busy}
                  onClick={() => act(() => reanalyzeLabs(id))}>Re-analyze</button></p>
      ) : (
        <>
          <p className="muted" style={{ fontSize: 12, margin: "2px 0 8px" }}>
            {s.results.length} results · {analytes.length} analytes
            {s.skipped ? ` · ${s.skipped} skipped (not in document)` : ""}
            {approved ? " · in your trends" : " — review before approving"}
          </p>
          {analytes.length > 0 && (
            <select className="med-dest-select" value={sel} onChange={(e) => setSel(e.target.value)} style={{ marginBottom: 6 }}>
              {analytes.map((a) => <option key={a.analyte} value={a.analyte}>{a.name} ({a.n})</option>)}
            </select>
          )}
          {series && series.points.length > 0 && <LabChart series={series} height={180} />}
          <details className="lab-table" style={{ marginTop: 6 }}>
            <summary className="muted" style={{ fontSize: 12 }}>Show all {s.results.length} as table</summary>
            <table>
              <thead><tr><th>Analyte</th><th>n</th><th>Latest</th></tr></thead>
              <tbody>
                {analytes.map((a) => (
                  <tr key={a.analyte}><td>{a.name}</td><td>{a.n}</td><td>{a.latest}{a.unit ? " " + a.unit : ""} <span className="muted">({a.at})</span></td></tr>
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
      .then((a) => setAtts(a.filter((x) => /pdf/i.test(x.mime) || /\.pdf$/i.test(x.filename))))
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
