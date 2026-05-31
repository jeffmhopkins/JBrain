import { useEffect, useState } from "react";
import { del, get, post, put } from "../api";
import Modal from "../components/Modal";
import ConfigFields from "../components/ConfigFields";

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
  name: "New trigger",
  trigger_type: "event",
  trigger_config: { event: "log_appended" },
  action_type: "append_to_note",
  action_config: { title: "Notes", text: "hello" },
  enabled: true,
};

interface ActionType { type: string; config: any[]; }

export default function WorkflowsPage() {
  const [items, setItems] = useState<Workflow[]>([]);
  const [editing, setEditing] = useState<any | null>(null);
  const [runs, setRuns] = useState<Record<number, any[]>>({});
  const [error, setError] = useState("");
  const [actionTypes, setActionTypes] = useState<ActionType[]>([]);

  async function load() {
    try { setItems(await get("/api/workflows")); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, []);
  // Action types are data-driven from the server catalog (YAML defs + Python),
  // so newly-added actions appear automatically.
  useEffect(() => { get("/api/workflows/action-types").then(setActionTypes).catch(() => {}); }, []);

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
    if (confirm(`Delete trigger “${w.name}”?`)) { await del(`/api/workflows/${w.id}`); load(); }
  }
  async function syncRepo() {
    const r = await post("/api/workflows/sync");
    setError(`Synced ${r.synced} workflow(s) from repo.`);
    load();
  }
  async function resetToRepo(w: Workflow) {
    await post(`/api/workflows/${w.id}/reset`);
    load();
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
    let triggerCfg: any, actionCfg: any;
    try { triggerCfg = JSON.parse(editing.trigger_config); }
    catch { setError("Trigger config isn’t valid JSON — fix it and try again."); return; }
    try { actionCfg = JSON.parse(editing.action_config); }
    catch { setError("Action config isn’t valid JSON — fix it and try again."); return; }
    const payload: any = {
      name: editing.name, trigger_type: editing.trigger_type,
      trigger_config: triggerCfg, action_type: editing.action_type,
      action_config: actionCfg, enabled: editing.enabled,
    };
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
        <h2 style={{ margin: 0 }}>Triggers</h2>
        <div className="spacer" />
        <button className="ghost" onClick={syncRepo} title="Pull new/updated workflows from the repo">Sync from repo</button>
        <button className="primary" onClick={() => openEdit()}>+ New</button>
      </div>
      <p className="muted" style={{ fontSize: 13 }}>
        A trigger runs an action on a schedule or event. Seeded from repo YAML; edits here lock a trigger from repo updates.
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
          <div className="wf-actions">
            <button className="ghost" onClick={() => toggle(w)}>{w.enabled ? "Disable" : "Enable"}</button>
            <button className="ghost" onClick={() => runNow(w)}>Run now</button>
            <button className="ghost" onClick={() => openEdit(w)}>Edit</button>
            <button className="ghost" onClick={() => showRuns(w.id)}>History</button>
            {w.locked && <button className="ghost" onClick={() => resetToRepo(w)} title="Discard local edits; track the repo version again">Reset to repo</button>}
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
        <Modal
          title={`${editing.id ? "Edit" : "New"} trigger`}
          size="wide"
          onClose={() => setEditing(null)}
          footer={<>
            <button className="primary" onClick={save}>Save</button>
            <button className="ghost" onClick={() => setEditing(null)}>Cancel</button>
            {error && <span className="spacer" />}
            {error && <span style={{ color: "var(--danger)", fontSize: 13 }}>{error}</span>}
          </>}
        >
          <label className="muted">Name</label>
          <input value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
          <div className="row" style={{ gap: 8, marginTop: 12 }}>
            <div style={{ flex: 1 }}>
              <label className="muted">Trigger type</label>
              <select className="modal-select" value={editing.trigger_type} onChange={(e) => setEditing({ ...editing, trigger_type: e.target.value })}>
                <option value="event">event</option>
                <option value="schedule">schedule</option>
              </select>
            </div>
            <div style={{ flex: 1 }}>
              <label className="muted">Action type</label>
              <select className="modal-select" value={editing.action_type} onChange={(e) => setEditing({ ...editing, action_type: e.target.value })}>
                {(actionTypes.length ? actionTypes.map((a) => a.type) : [editing.action_type]).map((t) => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
          </div>
          <label className="muted" style={{ marginTop: 12, display: "block" }}>Trigger config (JSON)</label>
          <textarea className="wf-textarea-lg" value={editing.trigger_config}
                    onChange={(e) => setEditing({ ...editing, trigger_config: e.target.value })} />

          <div className="muted" style={{ marginTop: 14, fontWeight: 600, color: "var(--text)" }}>Action settings</div>
          <ConfigFields
            schema={actionTypes.find((a) => a.type === editing.action_type)?.config || []}
            value={editing.action_config}
            onChange={(s) => setEditing({ ...editing, action_config: s })}
          />
          <details style={{ marginTop: 12 }}>
            <summary className="muted" style={{ fontSize: 13, cursor: "pointer" }}>Advanced — raw JSON</summary>
            <textarea className="wf-textarea-lg" style={{ marginTop: 8 }} value={editing.action_config}
                      onChange={(e) => setEditing({ ...editing, action_config: e.target.value })} />
          </details>

          <label className="row" style={{ marginTop: 12, gap: 8 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={editing.enabled}
                   onChange={(e) => setEditing({ ...editing, enabled: e.target.checked })} /> Enabled
          </label>
        </Modal>
      )}
    </div>
  );
}
