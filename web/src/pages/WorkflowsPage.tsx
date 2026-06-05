import { useEffect, useRef, useState } from "react";
import { del, get, post, put } from "../api";
import { useAuth } from "../App";
import { useNowTick } from "../hooks";
import { fmtTs, fmtElapsed, parseUtcMs } from "../time";
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

// Friendly labels for the live watch modal — one per pipeline primitive so a running
// step always reads as plain English ("Designing the taxonomy") instead of a raw
// `snake_case` name. Keep in sync with _PRIMITIVES in server/app/services/pipeline.py;
// any name not listed is humanized (underscores → spaces, capitalized).
const STEP_LABELS: Record<string, string> = {
  // Analysis & entities
  analyze_pending: "Finding notes to analyze", analyze_note: "Analyzing note",
  rebuild_entity_index: "Rebuilding the entity index", write_disambiguation: "Writing disambiguation pages",
  // Knowledge-base build
  kb_reset: "Clearing old knowledge base", corpus_digest: "Surveying notes",
  wiki_outline: "Designing the taxonomy", wiki_write_batch: "Writing articles",
  validate_structure: "Checking structure", write_kb_index: "Writing the index",
  link_owner: "Linking your profile", link_medications: "Linking medication references",
  link_places: "Reconciling saved places", flag_dead_links: "Checking cross-links",
  tidy_talk: "Tidying article notes", flag_ungrounded_reference: "Flagging ungrounded references",
  seed_kb_watermark: "Setting the watermark", review_open_talk: "Opening review items",
  record_talk: "Recording article notes",
  // Incremental KB upkeep
  query_entry_changes: "Finding changed entries", wiki_plan: "Planning updates",
  wiki_update: "Folding in changed notes", wiki_maintain: "Maintaining articles",
  validate_citations: "Checking citations", stage_kb_proposals: "Staging proposals",
  kb_uncited_pending: "Finding uncited entries", mark_evaluated: "Marking entries done",
  kb_audit: "Auditing the knowledge base", kb_old_citation_pending: "Finding old citations",
  recite_articles: "Restyling citations", check_needed_links: "Adding missing links",
  // Articles (manual ops)
  rebuild_article: "Rebuilding article", create_article: "Creating article",
  recategorize_article: "Recategorizing article", merge_articles: "Merging articles",
  split_article: "Splitting article", research_article: "Researching references",
  refresh_index: "Refreshing the index", taxonomy_health: "Checking taxonomy health",
  // Recurrence / chatter
  chatter_pending: "Gathering loose notes", cluster_chatter: "Finding patterns",
  mark_promoted: "Marking promoted",
  // Daily / day-log
  daily_pending: "Finding days to summarize", daylog_pending: "Reading the day log",
  summarise_entries: "Summarizing entries",
  // Filing & notes
  find_unfiled: "Finding unfiled notes", plan_moves: "Planning where to file",
  stage_moves: "Staging moves", redate_notes: "Filing by date", title_notes: "Titling notes",
  set_tags: "Tagging the note", suggest_tags: "Suggesting tags", write_note: "Saving note",
  // Places / location
  suggest_places: "Suggesting places", stage_places: "Staging places",
  discover_stays: "Discovering frequent spots", notify: "Sending a notification",
  research_nudges: "Finding research nudges",
  // Generic
  llm: "Asking the AI", gather_context: "Gathering context", semantic_search: "Searching notes",
  query_notes: "Reading notes", read_note: "Reading a note", create_review: "Posting review",
  call_action: "Running sub-action", get_meta: "Reading settings", set_meta: "Updating watermark",
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
  // baseMs/baseAt let us tick how long the CURRENT step has run (server-clock elapsed
  // captured at the last poll, advanced locally) so a long step doesn't look frozen.
  type Live = { events: string[]; status: string; detail: string; baseMs?: number; baseAt?: number };
  const [live, setLive] = useState<Record<number, Live>>({});
  const [watch, setWatch] = useState<{ id: number; name: string } | null>(null);
  const watchRunning = !!(watch && (running[watch.id] || live[watch.id]?.status === "running"));
  const nowTick = useNowTick(watchRunning, 1000);   // re-render once a second while watching

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
    let fails = 0;   // consecutive poll failures — back off and retry, don't give up on a blip
    const tick = async () => {
      if (!mounted.current) { polling.current.delete(id); return; }
      try {
        const s = await get<{ status: string; detail: string; events?: string[]; step_since?: string | null; now?: string | null }>(`/api/workflows/${id}/run-status`);
        fails = 0;
        const since = parseUtcMs(s.step_since), srvNow = parseUtcMs(s.now);
        const baseMs = isFinite(since) && isFinite(srvNow) ? Math.max(0, srvNow - since) : undefined;
        setLive((m) => ({ ...m, [id]: { events: s.events || [], status: s.status, detail: s.detail || "", baseMs, baseAt: Date.now() } }));
        if (s.status === "running") { setTimeout(tick, 800); return; }
        polling.current.delete(id);
        if (!mounted.current) return;
        setResult((m) => ({ ...m, [id]: { status: s.status, detail: s.detail || "" } }));
        setRunning((m) => ({ ...m, [id]: false }));
        showRuns(id); load();
      } catch {
        // A status poll can fail transiently exactly when the run is busiest (a long
        // synthesis hammers the server, or a deploy restarts it). Don't abandon the
        // watch on a single blip — back off and keep trying; give up only after a
        // sustained outage so a genuinely dead server doesn't spin forever.
        if (++fails > 20 || !mounted.current) {
          polling.current.delete(id);
          if (mounted.current) setRunning((m) => ({ ...m, [id]: false }));
          return;
        }
        setTimeout(tick, Math.min(800 * fails, 6000));
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
        const lv: Live = live[watch.id] || { events: [], status: "running", detail: "" };
        const isRunning = lv.status === "running" || running[watch.id];
        // How long the current step has been running: server-clock elapsed at the last
        // poll, advanced by the local 1s tick. Lets a long step show "· 1m 20s" instead
        // of looking frozen (the "it just stops" confusion).
        const stepElapsed = lv.baseMs != null && lv.baseAt != null ? lv.baseMs + (nowTick - lv.baseAt) : null;
        // Collapse consecutive identical steps (e.g. 30× "Saving note") into one row with a
        // count. Per-article writes arrive as "wiki_write:i/n:title" — fold them into one
        // "Writing articles" row that shows the article currently being written.
        const groups: { name: string; count: number; detail?: string }[] = [];
        for (const ev of lv.events) {
          let name = ev, detail: string | undefined;
          if (ev.startsWith("wiki_write:")) {
            const rest = ev.slice("wiki_write:".length);
            const sep = rest.indexOf(":");
            const prog = sep >= 0 ? rest.slice(0, sep) : "";
            const title = sep >= 0 ? rest.slice(sep + 1) : rest;
            name = "wiki_write_batch";
            detail = `${title}${prog ? ` (${prog})` : ""}`;
          }
          const last = groups[groups.length - 1];
          if (last && last.name === name) { last.count++; if (detail) last.detail = detail; }
          else groups.push({ name, count: 1, detail });
        }
        return (
          <Modal
            title={`Running: ${watch.name}`}
            size="compact"
            onClose={() => setWatch(null)}
            footer={<button className="ghost" onClick={() => setWatch(null)}>{isRunning ? "Run in background" : "Close"}</button>}
          >
            <div className={"run-status " + (isRunning ? "running" : lv.status === "ok" ? "ok" : "err")}>
              {isRunning ? <><span className="spinner" /> Running…</> : lv.status === "ok" ? "✓ Completed" : "✕ Failed"}
            </div>
            <ol className="watch-steps">
              {groups.map((g, i) => {
                const stepRunning = isRunning && i === groups.length - 1;
                return (
                  <li key={i} className={stepRunning ? "running" : "done"}>
                    <span className="watch-ico">{stepRunning ? "⏳" : "✓"}</span>
                    <span>
                      {prettyStep(g.name)}
                      {g.name === "wiki_write_batch" && g.detail ? ` — ${g.detail}`
                        : g.count > 1 ? ` ×${g.count}` : ""}
                    </span>
                    {stepRunning && stepElapsed != null && (
                      <span className="watch-elapsed">{fmtElapsed(stepElapsed)}</span>
                    )}
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
