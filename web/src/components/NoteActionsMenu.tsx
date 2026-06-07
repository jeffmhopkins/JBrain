import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";

// One actionable row in the kebab menu. A `{ sep: true }` entry renders a divider.
export type MenuItem =
  | { sep: true }
  | {
      key: string;
      label: string;
      icon: string;
      hint?: string;
      accent?: boolean;   // tint the tile with the house accent (the primary action)
      danger?: boolean;   // destructive (Delete) — danger-colored tile + label
      onClick: () => void;
    };

function isAction(i: MenuItem): i is Extract<MenuItem, { key: string }> {
  return !("sep" in i);
}

// The single "⋮" actions menu that replaces the inline Share/Edit/Delete buttons.
// Borrows the .mode-menu row grammar but drops DOWN, right-aligned, and adds the
// outside-click / Escape close + keyboard nav that the chat mode-picker lacks.
export default function NoteActionsMenu({ items, label = "Page actions" }: { items: MenuItem[]; label?: string }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const actions = items.filter(isAction);

  // Close on outside click (mousedown so it beats the row's onClick) and on Escape;
  // Escape returns focus to the trigger so keyboard users aren't stranded.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") { setOpen(false); triggerRef.current?.focus(); }
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // On open, focus the first item so arrow keys / Enter work immediately.
  useEffect(() => {
    if (open) wrapRef.current?.querySelector<HTMLButtonElement>(".mode-row")?.focus();
  }, [open]);

  function onMenuKey(e: React.KeyboardEvent) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const rows = Array.from(wrapRef.current?.querySelectorAll<HTMLButtonElement>(".mode-row") || []);
    const idx = rows.indexOf(document.activeElement as HTMLButtonElement);
    const next = e.key === "ArrowDown"
      ? rows[(idx + 1) % rows.length]
      : rows[(idx - 1 + rows.length) % rows.length];
    next?.focus();
  }

  function run(fn: () => void) { setOpen(false); fn(); }

  return (
    <span className="mode-wrap" ref={wrapRef}>
      <button ref={triggerRef} className={"icon-btn" + (open ? " active" : "")}
              aria-haspopup="menu" aria-expanded={open} aria-label={label}
              onClick={() => setOpen((o) => !o)}>
        <Icon name="dots" size={20} className="kebab-dots" />
      </button>
      {open && (
        <div className="note-menu" role="menu" aria-label={label} onKeyDown={onMenuKey}>
          {items.map((it, i) =>
            isAction(it) ? (
              <button key={it.key} role="menuitem"
                      className={"mode-row" + (it.accent ? " accent" : "") + (it.danger ? " danger" : "")}
                      onClick={() => run(it.onClick)}>
                <span className="mode-tile"><Icon name={it.icon} size={18} /></span>
                <span className="mode-label">{it.label}</span>
                {it.hint && <span className="mode-hint">{it.hint}</span>}
              </button>
            ) : (
              <div key={`sep-${i}`} className="note-menu-sep" role="separator" />
            ),
          )}
        </div>
      )}
    </span>
  );
}
