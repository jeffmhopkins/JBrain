import { FormEvent, useRef, useState } from "react";
import { downloadBackup, downloadOriginalNotes, post, restoreBackup } from "../api";

// The note selection behind the "Export original notes" button: each live note that is
// user-authored OR a guided intake, excluding the synthesized KB, with its ORIGINAL content
// (first user version, else earliest). The button's JSON also embeds attachments (base64),
// which SQL can't show — this preview is just the note rows.
const ORIGINAL_NOTES_SQL =
  "SELECT nv.title, nv.content_md, nv.created_at FROM notes n " +
  "JOIN note_versions nv ON nv.id = COALESCE(" +
  "(SELECT MIN(id) FROM note_versions u WHERE u.note_id=n.id AND u.source='user')," +
  "(SELECT MIN(id) FROM note_versions u WHERE u.note_id=n.id)) " +
  "WHERE n.deleted_at IS NULL AND n.kind != 'kb' AND (" +
  "EXISTS (SELECT 1 FROM note_versions u WHERE u.note_id=n.id AND u.source='user') " +
  "OR n.id IN (SELECT note_id FROM share_links WHERE kind='guided' AND note_id IS NOT NULL)) " +
  "ORDER BY nv.created_at";

const EXAMPLES = [
  "SELECT title, updated_at FROM notes WHERE deleted_at IS NULL ORDER BY updated_at DESC",
  "SELECT target_title, COUNT(*) AS refs FROM links GROUP BY target_title ORDER BY refs DESC",
  "SELECT COUNT(*) AS notes FROM notes WHERE deleted_at IS NULL",
  ORIGINAL_NOTES_SQL,
];

export default function SqlConsole() {
  const [sql, setSql] = useState(EXAMPLES[0]);
  const [columns, setColumns] = useState<string[]>([]);
  const [rows, setRows] = useState<any[][]>([]);
  const [error, setError] = useState("");
  const [backupMsg, setBackupMsg] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  async function doBackup() {
    setBackupMsg("Preparing download…");
    try { await downloadBackup(); setBackupMsg("Backup downloaded."); }
    catch (e: any) { setBackupMsg(`Backup failed: ${e.message}`); }
  }

  async function doExportOriginal() {
    setBackupMsg("Exporting original notes…");
    try { await downloadOriginalNotes(); setBackupMsg("Original notes exported (JSON)."); }
    catch (e: any) { setBackupMsg(`Export failed: ${e.message}`); }
  }

  async function doRestore(file: File) {
    if (!confirm(`Restore from “${file.name}”? This REPLACES your entire current database. Make a backup first.`)) return;
    setBackupMsg("Restoring…");
    try {
      await restoreBackup(file);
      setBackupMsg("Restored. Reloading…");
      setTimeout(() => location.reload(), 800);
    } catch (e: any) {
      setBackupMsg(`Restore failed: ${e.message}`);
    }
  }

  async function run(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      const r = await post("/api/sql", { sql });
      setColumns(r.columns || []);
      setRows(r.rows || []);
    } catch (err: any) {
      setError(err.message);
      setColumns([]); setRows([]);
    }
  }

  return (
    <div className="content">
      <h2>Database</h2>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Backup &amp; restore</h3>
        <p className="muted" style={{ fontSize: 13 }}>
          Your whole brain — notes, attachments, history, triggers — is one SQLite file.
          Export it to back up; import a backup to restore (replaces everything).
        </p>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <button className="primary" onClick={doBackup}>Export database</button>
          <input ref={fileRef} type="file" accept=".db,.sqlite,application/octet-stream"
                 style={{ display: "none" }}
                 onChange={(e) => { const f = e.target.files?.[0]; if (f) doRestore(f); e.currentTarget.value = ""; }} />
          <button className="ghost" onClick={() => fileRef.current?.click()}>Import database…</button>
          {backupMsg && <span className="muted" style={{ fontSize: 13 }}>{backupMsg}</span>}
        </div>
        <p className="muted" style={{ fontSize: 13, marginTop: 12, marginBottom: 4 }}>
          Or export <strong>just your notes and guided intakes</strong>, with their
          attachments — your original content, before any AI edit or rename, and without the
          synthesized KB articles (JSON, attachments embedded).
        </p>
        <button className="ghost" onClick={doExportOriginal}>Export original notes</button>
      </div>

      <h3>SQL console</h3>
      <p className="muted">Read-only access to your brain’s database (SELECT/WITH only). For full SQL use the <code>sqlite3</code> CLI — see the README.</p>
      <form onSubmit={run}>
        <textarea rows={4} value={sql} onChange={(e) => setSql(e.target.value)} style={{ fontFamily: "monospace" }} />
        <div className="row" style={{ marginTop: 10 }}>
          <button className="primary" type="submit">Run query</button>
          <div className="spacer" />
        </div>
      </form>
      <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 10 }}>
        {EXAMPLES.map((ex) => (
          <button key={ex} className="ghost" style={{ fontSize: 12 }} onClick={() => setSql(ex)}>{ex.slice(0, 38)}…</button>
        ))}
      </div>
      {error && <p style={{ color: "var(--danger)" }}>{error}</p>}
      {columns.length > 0 && (
        <div style={{ overflowX: "auto", marginTop: 16 }}>
          <table className="sql-result">
            <thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>{row.map((cell, j) => <td key={j}>{String(cell)}</td>)}</tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 12 }}>{rows.length} row(s)</p>
        </div>
      )}
    </div>
  );
}
