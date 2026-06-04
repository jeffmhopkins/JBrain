import { useEffect, useRef, useState } from "react";
import { del, get, post, put } from "../api";
import { useAuth } from "../App";
import { fmtTs } from "../time";
import Modal from "../components/Modal";
import ConfigFields from "../components/ConfigFields";
import { Icon } from "../components/Icon";

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

interface ActionType { type: string; config: any[]; category?: string; }

const CAT_ORDER = ["Knowledge base", "Daily", "Notes", "Utility", "Other"];
function byCategory<T>(list: T[], catOf: (x: T) => string): [string, T[]][] {
  const map = new Map<string, T[]>();
  for (const x of list) {
    const c = catOf(x) || "Other";
    (map.get(c) ?? map.set(c, []).get(c)!).push(x);
  }
  const rank = (c: string) => { const i = CAT_ORDER.indexOf(c); return i === -1 ? 99 : i; };
  return [...map.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
    .map((c) => [c, map.get(c)!] as [string, T[]]);
}

// Friendly labels for the live watch modal; unknown steps are humanized.
const STEP_LABELS: Record<string, string> = {
  analyze_pending: "Finding notes to analyze", analyze_note: "Analyzing note",
  kb_reset: "Clearing old knowledge base", corpus_digest: "Surveying notes",
  wiki_outline: "Designing the taxonomy", wiki_write_batch: "Writing articles",
  validate_structure: "Checking structure", write_note: "Saving note",
  create_review: "Posting review", call_action: "Running sub-action",
  query_entry_changes: "Finding changed entries", wiki_plan: "Planning updates",
  validate_citations: "Checking citations", set_meta: "Updating watermark",
};
const prettyStep = (n: string) => STEP_LABELS[n] || n.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export default function WorkflowsPage() {
  const { appTz } = useAuth();
  const [items, setItems] = useState<Workflow[]>([]);
  const [editing, setEditing] = useState<any | null>(null);
  const [runs, setRuns] = useState<Record<number, any[]>>({});
  const [running, setRunning] = useState<Record<number, boolean>>({});
  const [result, setResult] = useState<Record<number, { status: string; detail: string }>>({});
  const [error, setError] = useState("");
  const [actionTypes, setActionTypes] = useState<ActionType[]>([]);
  // Live step trace per workflow (for the watch modal) + which run we're watching.
  const [live, setLive] = useState<Record<number, { events: string[]; status: string; detail: string }>>({});
  const [watch, setWatch] = useState<{ id: number; name: string } | null>(null);

  async function load() {
    try { setItems(await get("/api/workflows")); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, []);
  // Action types are data-driven from the server catalog (YAML defs + Python),
  // so newly-added actions appear automatically.
  useEffect(() => { get("/api/workflows/action-types").then(setActionTypes).catch(() => {}); }, []);

  const mounted = useRef(true);
  const polling = useRef<Set<number>>(new Set());
  useEffect(() => () => { mounted.current = false; }, []);

  async function toggle(w: Workflow) { await post(`/api/workflows/${w.id}/toggle`); load(); }

  async function runNow(w: Workflow) {
    setRunning((m) => ({ ...m, [w.id]: true }));
    setResult((m) => { const n = { ...m }; delete n[w.id]; return n; });
    setError("");
    setLive((m) => ({ ...m, [w.id]: { events: [], status: "running", detail: "" } }));
    setWatch({ id: w.id, name: w.name });          // open the live watch modal
    try {
      await post(`/api/workflows/${w.id}/run`);   // kicks off in the background
      pollRun(w.id);
    } catch (e: any) {
      setResult((m) => ({ ...m, [w.id]: { status: "error", detail: e?.message || "request failed" } }));
      setRunning((m) => ({ ...m, [w.id]: false }));
    }
  }

  // Poll the run's status until it leaves 'running', then show the result.
  function pollRun(id: number) {
    if (polling.current.has(id)) return;
    polling.current.add(id);
    setRunning((m) => ({ ...m, [id]: true }));
    const tick = async () => {
      if (!mounted.current) { polling.current.delete(id); return; }
      try {
        const s = await get<{ status: string; detail: string; events?: string[] }>(`/api/workflows/${id}/run-status`);
        setLive((m) => ({ ...m, [id]: { events: s.events || [], status: s.status, detail: s.detail || "" } }));
        if (s.status === "running") { setTimeout(tick, 800); return; }
        polling.current.delete(id);
        if (!mounted.current) return;
        setResult((m) => ({ ...m, [id]: { status: s.status, detail: s.detail || "" } }));
        setRunning((m) => ({ ...m, [id]: false }));
        showRuns(id); load();
      } catch {
        polling.current.delete(id);
        if (mounted.current) setRunning((m) => ({ ...m, [id]: false }));
      }
    };
    tick();
  }

  // Resume the indicator if a run is still in flight (e.g. after a page reload).
  useEffect(() => {
    for (const w of items) if (w.last_status === "running") pollRun(w.id);
  }, [items]);
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
      {byCategory(items, (w) => actionTypes.find((a) => a.type === w.action_type)?.category || "Other").map(([cat, group]) => (
        <div key={cat}>
          <div className="adv-section">{cat}</div>
          {group.map((w) => (
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
            <button className="ghost" onClick={() => runNow(w)} disabled={running[w.id]}>
              {running[w.id] ? "Running…" : "Run now"}
            </button>
            {live[w.id] && (
              <button className="ghost" onClick={() => setWatch({ id: w.id, name: w.name })}>
                {running[w.id] ? "⏳ Watch" : "View last run"}
              </button>
            )}
            <button className="ghost" onClick={() => openEdit(w)}>Edit</button>
            <button className="ghost" onClick={() => showRuns(w.id)}>History</button>
            {w.locked && <button className="ghost" onClick={() => resetToRepo(w)} title="Discard local edits; track the repo version again">Reset to repo</button>}
            <button className="ghost" onClick={() => remove(w)}>Delete</button>
          </div>
          {(running[w.id] || result[w.id]) && (
            <div className={"run-status " + (running[w.id] ? "running" : result[w.id].status === "ok" ? "ok" : "err")}>
              {running[w.id] ? (
                <><span className="spinner" /> Running…</>
              ) : result[w.id].status === "ok" ? (
                <><Icon name="check" size={14} /> {result[w.id].detail || "Done"}</>
              ) : (
                <><Icon name="bell" size={14} /> {result[w.id].detail || "Failed"}</>
              )}
            </div>
          )}
          {runs[w.id] && (
            <div style={{ marginTop: 8, fontSize: 12 }}>
              {runs[w.id].length === 0 && <span className="muted">No runs.</span>}
              {runs[w.id].map((r) => (
                <div key={r.id} className="muted">{fmtTs(r.started_at, appTz)} · {r.status} · {r.detail}</div>
              ))}
            </div>
          )}
        </div>
          ))}
        </div>
      ))}

      {watch && (() => {
        const lv = live[watch.id] || { events: [], status: "running", detail: "" };
        const isRunning = lv.status === "running" || running[watch.id];
        // Collapse consecutive identical steps (e.g. 30× "Saving note") into one row with a count.
        const groups: { name: string; count: number }[] = [];
        for (const name of lv.events) {
          const last = groups[groups.length - 1];
          if (last && last.name === name) last.count++; else groups.push({ name, count: 1 });
        }
        return (
          <Modal
            title={`Running: ${watch.name}`}
            size="compact"
            onClose={() => setWatch(null)}
            footer={<button className="ghost" onClick={() => setWatch(null)}>{isRunning ? "Run in background" : "Close"}</button>}
          >
            <div className={"run-status " + (isRunning ? "running" : lv.status === "ok" ? "ok" : "err")}>
              {isRunning ? "Running…" : lv.status === "ok" ? "✓ Completed" : "✕ Failed"}
            </div>
            <ol className="watch-steps">
              {groups.map((g, i) => {
                const stepRunning = isRunning && i === groups.length - 1;
                return (
                  <li key={i} className={stepRunning ? "running" : "done"}>
                    <span className="watch-ico">{stepRunning ? "⏳" : "✓"}</span>
                    {prettyStep(g.name)}{g.count > 1 ? ` ×${g.count}` : ""}
                  </li>
                );
              })}
              {groups.length === 0 && <li className="muted">Starting…</li>}
            </ol>
            {!isRunning && lv.detail && <p className="muted watch-detail">{lv.detail}</p>}
          </Modal>
        );
      })()}

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
