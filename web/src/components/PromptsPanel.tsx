import { useEffect, useState } from "react";
import { del, get, put } from "../api";
import Modal from "./Modal";
import { Icon } from "./Icon";

interface PromptRow {
  key: string;
  default: string;
  override: string | null;
  effective: string;
}

// Grouped, filterable list (matches the Wiki .tool page) → a Modal editor per
// prompt. Replaces the old wall of always-open textareas.
const GROUPS: { id: string; label: string }[] = [
  { id: "modes", label: "Modes" },
  { id: "tools", label: "Tools" },
  { id: "actions", label: "Actions" },
  { id: "agent", label: "Agent" },
];

function friendlyLabel(key: string): string {
  const parts = key.split(".");
  // modes.<mode>.system → the mode name (both end in "system", so use the mode).
  const last = parts[0] === "modes" && parts[parts.length - 1] === "system" ? parts[1] : parts[parts.length - 1];
  const s = last.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}
const groupOf = (key: string) => key.split(".")[0];
// agent.model is a one-line id; modes/actions are long prompts (big editor);
// tool descriptions are full sentences → a medium textarea, not a single line.
function fieldKind(key: string): "line" | "area" | "big" {
  if (key === "agent.model") return "line";
  if (key.startsWith("modes.") || key.startsWith("actions.")) return "big";
  return "area";
}

export default function PromptsPanel() {
  const [rows, setRows] = useState<PromptRow[]>([]);
  const [filter, setFilter] = useState("");
  const [modifiedOnly, setModifiedOnly] = useState(false);
  const [editing, setEditing] = useState<PromptRow | null>(null);
  const [draft, setDraft] = useState("");
  // Collapsible sections; Modes open by default.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(["tools", "actions", "agent"]));
  const toggleGroup = (id: string) => setCollapsed((s) => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n;
  });

  async function load() {
    try { setRows(await get<PromptRow[]>("/api/prompts")); } catch { /* ignore */ }
  }
  useEffect(() => { load(); }, []);

  function openEdit(r: PromptRow) { setEditing(r); setDraft(r.effective); }
  function guardedClose() {
    if (editing && draft !== editing.effective && !confirm("Discard changes?")) return;
    setEditing(null);
  }
  async function save() {
    if (!editing) return;
    await put(`/api/prompts/${encodeURIComponent(editing.key)}`, { value: draft });
    setEditing(null);
    load();
  }
  async function resetCurrent() {
    if (!editing || !confirm(`Reset “${friendlyLabel(editing.key)}” to the shipped default?`)) return;
    await del(`/api/prompts/${encodeURIComponent(editing.key)}`);
    setEditing(null);
    load();
  }

  const q = filter.trim().toLowerCase();
  const visible = rows.filter((r) =>
    (!modifiedOnly || r.override !== null) &&
    (!q || friendlyLabel(r.key).toLowerCase().includes(q) || r.key.toLowerCase().includes(q) || r.effective.toLowerCase().includes(q)),
  );

  return (
    <div className="tool">
      <div className="tool-bar">
        <input className="tool-filter" placeholder="Filter prompts…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <button className={modifiedOnly ? "primary" : "ghost"} onClick={() => setModifiedOnly((v) => !v)}>Modified</button>
      </div>

      <div className="tool-body">
        {visible.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No matching prompts.</p>}
        {GROUPS.map((g) => {
          const items = visible.filter((r) => groupOf(r.key) === g.id)
            .sort((a, b) => friendlyLabel(a.key).localeCompare(friendlyLabel(b.key)));
          if (items.length === 0) return null;
          const open = !!q || !collapsed.has(g.id);   // a filter force-opens groups
          return (
            <div key={g.id}>
              <div className="adv-section prompt-section" onClick={() => toggleGroup(g.id)}>
                <span className={"chev" + (open ? " open" : "")}><Icon name="chevron" size={12} /></span>
                {g.label}
                <span className="prompt-count">{items.length}</span>
              </div>
              {open && items.map((r) => (
                <div className="list-item" style={{ cursor: "pointer" }} onClick={() => openEdit(r)} key={r.key}>
                  <div className="row">
                    <strong style={{ fontSize: 14 }}>{friendlyLabel(r.key)}</strong>
                    {r.override !== null && <span className="badge">overridden</span>}
                    <span className="spacer" />
                    <code className="muted" style={{ fontSize: 11 }}>{r.key}</code>
                  </div>
                  <div className="muted" style={{ fontSize: 12, marginTop: 4, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {r.effective || "(blank — uses provider default)"}
                  </div>
                </div>
              ))}
            </div>
          );
        })}
      </div>

      {editing && (
        <Modal
          title={friendlyLabel(editing.key)}
          size={fieldKind(editing.key) === "big" ? "wide" : "default"}
          headerExtra={editing.override !== null ? <span className="badge">overridden</span> : undefined}
          onClose={guardedClose}
          footer={<>
            <button className="primary" onClick={save} disabled={draft === editing.effective}>Save</button>
            {editing.override !== null && <button className="ghost" onClick={resetCurrent}>Reset to default</button>}
            <button className="ghost" onClick={guardedClose}>Cancel</button>
          </>}
        >
          <code className="muted" style={{ fontSize: 11 }}>{editing.key}</code>
          {(() => {
            const kind = fieldKind(editing.key);
            if (kind === "line") {
              return <input style={{ marginTop: 8 }} value={draft} onChange={(e) => setDraft(e.target.value)}
                            placeholder="Blank = use the provider default model" />;
            }
            if (kind === "area") {
              return <textarea style={{ marginTop: 8, minHeight: 130, resize: "vertical", fontFamily: "monospace", fontSize: 13 }}
                               value={draft} onChange={(e) => setDraft(e.target.value)} />;
            }
            return <textarea className="wf-textarea-lg" style={{ marginTop: 8, fontFamily: "monospace", fontSize: 13 }}
                             value={draft} onChange={(e) => setDraft(e.target.value)} />;
          })()}
          <details style={{ marginTop: 12 }}>
            <summary className="muted" style={{ fontSize: 12, cursor: "pointer" }}>Shipped default</summary>
            <pre style={{ marginTop: 6, whiteSpace: "pre-wrap", fontSize: 12, color: "var(--text-dim)" }}>{editing.default || "(blank)"}</pre>
          </details>
        </Modal>
      )}
    </div>
  );
}
