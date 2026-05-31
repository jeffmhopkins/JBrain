import { useEffect, useState } from "react";
import { del, get, post, put } from "../api";
import Modal from "../components/Modal";
import PipelineView from "../components/PipelineView";

interface ActionDef { type: string; source: string; locked: boolean; num_steps: number; summary: string; }
interface Editing {
  type: string; source: string; recipe_yaml: string; recipe: any;
  warnings: string[]; isNew: boolean; readOnly: boolean; error: string;
}

// Actions = the step recipes behind each action type. Shipped recipes are
// read-only (duplicate to a custom one to edit); custom recipes are editable as
// raw YAML with live server validation + a read-only pipeline visualisation.
export default function ActionsPage() {
  const [items, setItems] = useState<ActionDef[]>([]);
  const [editing, setEditing] = useState<Editing | null>(null);

  async function load() {
    try { setItems(await get("/api/action-defs")); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, []);

  async function open(type: string, opts: { duplicate?: boolean } = {}) {
    const d = await get(`/api/action-defs/${encodeURIComponent(type)}`);
    const readOnly = d.source === "repo" && !opts.duplicate;
    let yaml = d.recipe_yaml;
    let isNew = false;
    if (opts.duplicate) {
      isNew = true;
      yaml = yaml.replace(/(^|\n)type:[^\n]*/, `$1type: ${type}_copy`);
    }
    setEditing({ type: opts.duplicate ? `${type}_copy` : d.type, source: d.source, recipe_yaml: yaml,
                 recipe: d.recipe, warnings: d.warnings || [], isNew, readOnly, error: "" });
  }

  function openNew() {
    setEditing({
      type: "new_action", source: "user", isNew: true, readOnly: false, warnings: [], error: "",
      recipe_yaml: "type: my_action\nsteps:\n  - do: create_review\n    with:\n      title: Hello\n",
      recipe: { steps: [{ do: "create_review", with: { title: "Hello" } }] },
    });
  }

  async function validate() {
    if (!editing) return;
    try {
      const r = await post("/api/action-defs/validate", { recipe_yaml: editing.recipe_yaml });
      setEditing({ ...editing, recipe: r.recipe ?? editing.recipe, warnings: r.warnings, error: "" });
    } catch (e: any) {
      setEditing({ ...editing, error: e.message });
    }
  }

  async function save() {
    if (!editing) return;
    try {
      if (editing.isNew) await post("/api/action-defs", { recipe_yaml: editing.recipe_yaml });
      else await put(`/api/action-defs/${encodeURIComponent(editing.type)}`, { recipe_yaml: editing.recipe_yaml });
      setEditing(null);
      load();
    } catch (e: any) {
      setEditing({ ...editing, error: e.message });
    }
  }

  async function remove(d: ActionDef) {
    if (!confirm(`Delete custom action “${d.type}”?`)) return;
    try { await del(`/api/action-defs/${encodeURIComponent(d.type)}`); load(); }
    catch (e: any) { alert(e.message); }
  }

  const shipped = items.filter((d) => d.source === "repo");
  const custom = items.filter((d) => d.source === "user");

  const group = (label: string, list: ActionDef[]) => (
    <div>
      <div className="adv-section">{label}</div>
      {list.length === 0 && <p className="muted" style={{ fontSize: 13 }}>None.</p>}
      {list.map((d) => (
        <div className="card" key={d.type}>
          <div className="row">
            <strong>{d.type}</strong>
            {d.locked && <span className="badge">edited</span>}
            <span className="spacer" />
            <span className="badge">{d.num_steps} steps</span>
          </div>
          <div className="muted" style={{ fontSize: 12, margin: "4px 0" }}>{d.summary}</div>
          <div className="wf-actions">
            <button className="ghost" onClick={() => open(d.type)}>{d.source === "repo" ? "View" : "Edit"}</button>
            <button className="ghost" onClick={() => open(d.type, { duplicate: true })}>Duplicate</button>
            {d.source === "user" && <button className="ghost" onClick={() => remove(d)}>Delete</button>}
          </div>
        </div>
      ))}
    </div>
  );

  return (
    <div className="content">
      <div className="row">
        <h2 style={{ margin: 0 }}>Actions</h2>
        <div className="spacer" />
        <button className="ghost" onClick={() => post("/api/action-defs/sync").then(load)} title="Pull new/updated recipes from the repo">Sync from repo</button>
        <button className="primary" onClick={openNew}>+ New</button>
      </div>
      <p className="muted" style={{ fontSize: 13 }}>
        The step recipe behind each action type. <em>Prompts</em> = what the AI is told ·
        <em> Actions</em> = the recipe · <em>Triggers</em> = when it runs.
      </p>

      {group("Shipped", shipped)}
      {group("Custom", custom)}

      {editing && (
        <Modal
          title={editing.isNew ? "New action" : `${editing.readOnly ? "" : "Edit "}${editing.type}`}
          size="wide"
          onClose={() => setEditing(null)}
          footer={<>
            {!editing.readOnly && <button className="primary" onClick={save}>Save</button>}
            <button className="ghost" onClick={validate}>Validate</button>
            <button className="ghost" onClick={() => setEditing(null)}>Close</button>
            {editing.error && <><span className="spacer" /><span style={{ color: "var(--danger)", fontSize: 13 }}>{editing.error}</span></>}
          </>}
        >
          <div className="adv-section">Pipeline</div>
          <PipelineView steps={editing.recipe?.steps || []} />

          {editing.warnings.length > 0 && (
            <div className="card" style={{ borderColor: "var(--accent)" }}>
              <strong style={{ fontSize: 13 }}>Warnings</strong>
              {editing.warnings.map((w, i) => <div key={i} className="muted" style={{ fontSize: 12 }}>• {w}</div>)}
            </div>
          )}

          <div className="adv-section">{editing.readOnly ? "Recipe (YAML)" : "Recipe (YAML) — edit, then Validate"}</div>
          <textarea className="wf-textarea-lg" value={editing.recipe_yaml} readOnly={editing.readOnly}
                    onChange={(e) => setEditing({ ...editing, recipe_yaml: e.target.value })} />
          {editing.readOnly && (
            <p className="muted" style={{ fontSize: 12 }}>Shipped actions are read-only — use Duplicate to make an editable copy.</p>
          )}
        </Modal>
      )}
    </div>
  );
}
