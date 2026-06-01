import { ReactNode, useState } from "react";
import { Icon } from "./Icon";
import { Parsed } from "../lists";

// The shared list-card editor: interactive checkboxes, add, reorder, remove, and
// (optionally) the priority-queue toggle. Presentational — it computes the next
// Parsed model and hands it to onChange; the parent decides whether to persist
// immediately (Lists card) or buffer it (note edit, share proposal).
export default function ListEditor({ value, onChange, renderItemText, showQueueToggle = true }: {
  value: Parsed;
  onChange: (next: Parsed) => void;
  renderItemText?: (text: string) => ReactNode;
  showQueueToggle?: boolean;
}) {
  const [draft, setDraft] = useState("");

  function up(fn: (p: Parsed) => void) {
    const c: Parsed = { header: value.header, queue: value.queue, items: value.items.map((i) => ({ ...i })) };
    fn(c);
    onChange(c);
  }
  function add() {
    const t = draft.trim();
    if (!t) return;
    setDraft("");
    up((p) => { p.items.push({ checked: false, text: t, priority: null }); });
  }

  return (
    <div>
      {showQueueToggle && (
        <button className={"ghost queue-toggle" + (value.queue ? " on" : "")} style={{ fontSize: 12, marginBottom: 6 }}
                title="Order sets numeric priority (top = P1)"
                onClick={() => up((p) => { p.queue = !p.queue; })}>
          {value.queue ? "✓ Priority queue" : "Priority queue"}
        </button>
      )}
      <div className="checklist">
        {value.items.map((it, i) => (
          <div key={i} className="check-row">
            <label className={"check-item" + (it.checked ? " done" : "")}>
              <input type="checkbox" checked={it.checked}
                     onChange={(e) => up((p) => { p.items[i].checked = e.target.checked; })} />
              {value.queue ? <span className="badge prio">{i + 1}</span>
                : (it.priority != null && <span className="badge prio">P{it.priority}</span>)}
              <span className="check-text">{renderItemText ? renderItemText(it.text) : it.text}</span>
            </label>
            <span className="reorder">
              <button className="reorder-btn" title="Move up" disabled={i === 0}
                      onClick={() => up((p) => { [p.items[i - 1], p.items[i]] = [p.items[i], p.items[i - 1]]; })}><Icon name="chevron" size={14} /></button>
              <button className="reorder-btn down" title="Move down" disabled={i === value.items.length - 1}
                      onClick={() => up((p) => { [p.items[i + 1], p.items[i]] = [p.items[i], p.items[i + 1]]; })}><Icon name="chevron" size={14} /></button>
              <button className="ghost" style={{ padding: "2px 6px" }} title="Remove"
                      onClick={() => up((p) => { p.items.splice(i, 1); })}>✕</button>
            </span>
          </div>
        ))}
        {value.items.length === 0 && <span className="muted" style={{ fontSize: 13 }}>Empty list.</span>}
      </div>
      <div className="row" style={{ marginTop: 8, gap: 6 }}>
        <input placeholder="Add an item…" value={draft}
               onChange={(e) => setDraft(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
        <button className="ghost" onClick={add}>Add</button>
      </div>
    </div>
  );
}
