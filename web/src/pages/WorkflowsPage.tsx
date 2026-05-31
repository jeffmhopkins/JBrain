import { useEffect, useState } from "react";
import { del, get, post, put } from "../api";

interface Workflow {
  id: number;
  name: string;
  trigger_type: string;
  trigger_config: any;
  action_type: string;
  action_config: any;
  enabled: boolean;
  locked: boolean;
  source: string;
  last_status: string | null;
  last_run_at: string | null;
}

const BLANK = {
  name: "New workflow",
  trigger_type: "event",
  trigger_config: { event: "log_appended" },
  action_type: "append_to_note",
  action_config: { title: "Notes", text: "hello" },
  enabled: true,
};

export default function WorkflowsPage() {
  const [items, setItems] = useState<Workflow[]>([]);
  const [editing, setEditing] = useState<any | null>(null);
  const [runs, setRuns] = useState<Record<number, any[]>>({});
  const [error, setError] = useState("");

  async function load() {
    try { setItems(await get("/api/workflows")); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, []);

  async function toggle(w: Workflow) { await post(`/api/workflows/${w.id}/toggle`); load(); }
  async function runNow(w: Workflow) {
    const r = await post(`/api/workflows/${w.id}/run`);
    setError(r.status === "ok" ? "" : `Run failed: ${r.detail}`);
    showRuns(w.id);
  }
  async function showRuns(id: number) {
    const r = await get(`/api/workflows/${id}/runs`);
    setRuns((m) => ({ ...m, [id]: r }));
  }
  async function remove(w: Workflow) {
    if (confirm(`Delete workflow “${w.name}”?`)) { await del(`/api/workflows/${w.id}`); load(); }
  }

  function openEdit(w?: Workflow) {
    setError("");
    setEditing(w ? {
      id: w.id, name: w.name, trigger_type: w.trigger_type,
      trigger_config: JSON.stringify(w.trigger_config, null, 2),
      action_type: w.action_type, action_config: JSON.stringify(w.action_config, null, 2),
      enabled: w.enabled,
    } : {
      ...BLANK,
      trigger_config: JSON.stringify(BLANK.trigger_config, null, 2),
      action_config: JSON.stringify(BLANK.action_config, null, 2),
    });
  }

  async function save() {
    let payload: any;
    try {
      payload = {
        name: editing.name, trigger_type: editing.trigger_type,
        trigger_config: JSON.parse(editing.trigger_config),
        action_type: editing.action_type,
        action_config: JSON.parse(editing.action_config),
        enabled: editing.enabled,
      };
    } catch {
      setError("Trigger/action config must be valid JSON.");
      return;
    }
    try {
      if (editing.id) await put(`/api/workflows/${editing.id}`, payload);
      else await post("/api/workflows", payload);
      setEditing(null);
      load();
    } catch (e: any) {
      setError(e.message);
    }
  }

  return (
    <div className="content">
      <div className="row">
        <h2 style={{ margin: 0 }}>Workflows</h2>
        <div className="spacer" />
        <button className="primary" onClick={() => openEdit()}>+ New</button>
      </div>
      <p className="muted" style={{ fontSize: 13 }}>
        Automations (trigger → action). Seeded from repo YAML; edits here lock a workflow from repo updates.
      </p>
      {error && <p style={{ color: "var(--danger)" }}>{error}</p>}

      {items.length === 0 && <p className="muted">No workflows yet.</p>}
      {items.map((w) => (
        <div className="card" key={w.id}>
          <div className="row">
            <strong>{w.name}</strong>
            {w.locked && <span className="badge" title="Edited here; frozen from repo re-ingest">locked</span>}
            <span className="spacer" />
            <span className="badge">{w.trigger_type}</span>
            <span className={`badge badge-${w.enabled ? "architect" : "import"}`}>{w.enabled ? "on" : "off"}</span>
          </div>
          <div className="muted" style={{ fontSize: 12, margin: "4px 0" }}>
            {w.action_type}{w.last_status ? ` · last: ${w.last_status}` : ""}
          </div>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            <button className="ghost" onClick={() => toggle(w)}>{w.enabled ? "Disable" : "Enable"}</button>
            <button className="ghost" onClick={() => runNow(w)}>Run now</button>
            <button className="ghost" onClick={() => openEdit(w)}>Edit</button>
            <button className="ghost" onClick={() => showRuns(w.id)}>History</button>
            <button className="ghost" onClick={() => remove(w)}>Delete</button>
          </div>
          {runs[w.id] && (
            <div style={{ marginTop: 8, fontSize: 12 }}>
              {runs[w.id].length === 0 && <span className="muted">No runs.</span>}
              {runs[w.id].map((r) => (
                <div key={r.id} className="muted">{r.started_at} · {r.status} · {r.detail}</div>
              ))}
            </div>
          )}
        </div>
      ))}

      {editing && (
        <div className="overlay" onClick={() => setEditing(null)}>
          <div className="overlay-card" onClick={(e) => e.stopPropagation()}>
            <div className="row"><strong>{editing.id ? "Edit" : "New"} workflow</strong>
              <span className="spacer" /><button className="ghost" onClick={() => setEditing(null)}>Close</button></div>
            <label className="muted">Name</label>
            <input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            <div className="row" style={{ gap: 8, marginTop: 8 }}>
              <div style={{ flex: 1 }}>
                <label className="muted">Trigger type</label>
                <select value={editing.trigger_type} onChange={(e) => setEditing({ ...editing, trigger_type: e.target.value })}
                        style={{ width: "100%", padding: 9, background: "var(--bg-elev)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 8 }}>
                  <option value="event">event</option>
                  <option value="schedule">schedule</option>
                </select>
              </div>
              <div style={{ flex: 1 }}>
                <label className="muted">Action type</label>
                <select value={editing.action_type} onChange={(e) => setEditing({ ...editing, action_type: e.target.value })}
                        style={{ width: "100%", padding: 9, background: "var(--bg-elev)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 8 }}>
                  <option value="append_to_note">append_to_note</option>
                  <option value="claude_synthesize">claude_synthesize</option>
                </select>
              </div>
            </div>
            <label className="muted" style={{ marginTop: 8, display: "block" }}>Trigger config (JSON)</label>
            <textarea rows={3} style={{ fontFamily: "monospace" }} value={editing.trigger_config}
                      onChange={(e) => setEditing({ ...editing, trigger_config: e.target.value })} />
            <label className="muted" style={{ marginTop: 8, display: "block" }}>Action config (JSON)</label>
            <textarea rows={4} style={{ fontFamily: "monospace" }} value={editing.action_config}
                      onChange={(e) => setEditing({ ...editing, action_config: e.target.value })} />
            <label className="row" style={{ marginTop: 10, gap: 8 }}>
              <input type="checkbox" style={{ width: "auto" }} checked={editing.enabled}
                     onChange={(e) => setEditing({ ...editing, enabled: e.target.checked })} /> Enabled
            </label>
            <div className="row" style={{ marginTop: 14 }}>
              <button className="primary" onClick={save}>Save</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
