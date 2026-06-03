import { TouchEvent, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "../components/Icon";

// The Advanced launcher: a grid of cards, one per tool. Picking a card opens
// that tool full-screen (the Shell handles the back-to-grid chevron). Kept
// deliberately calm — flat cards, monochrome icons, one dim sub-label, no
// counts/badges — so it reads as the same app as the compose view.
interface Card { to: string; label: string; sub: string; icon: string; }

const SECTIONS: { name: string; cards: Card[] }[] = [
  {
    name: "Knowledge",
    cards: [
      { to: "/wiki", label: "Wiki", sub: "Browse & edit notes", icon: "wiki" },
      { to: "/lists", label: "Lists", sub: "Checklists & todos", icon: "list" },
      { to: "/search", label: "Search", sub: "Full-text & semantic", icon: "search" },
      { to: "/graph", label: "Graph", sub: "Connections", icon: "graph" },
      { to: "/map", label: "Map", sub: "Location trail & heatmap", icon: "pin" },
    ],
  },
  {
    name: "Authoring",
    cards: [
      { to: "/prompts", label: "Prompts", sub: "Tune the AI", icon: "robot" },
      { to: "/actions", label: "Actions", sub: "Step recipes", icon: "bolt" },
      { to: "/flows", label: "Triggers", sub: "When actions run", icon: "flows" },
    ],
  },
  {
    name: "System",
    cards: [
      { to: "/shares", label: "Shares", sub: "Public links · proposals", icon: "link" },
      { to: "/sql", label: "Data", sub: "SQL · backup", icon: "sql" },
      { to: "/system", label: "System", sub: "Version · settings", icon: "cog" },
    ],
  },
];

export default function AdvancedHome() {
  const nav = useNavigate();

  // Advanced is the last stop of the chat mode carousel; a horizontal swipe
  // returns to chat at the neighbouring mode (right → Research, left wraps → Entry).
  const start = useRef<{ x: number; y: number } | null>(null);
  function onTouchStart(e: TouchEvent) { const t = e.touches[0]; start.current = { x: t.clientX, y: t.clientY }; }
  function onTouchEnd(e: TouchEvent) {
    const s = start.current; start.current = null;
    if (!s) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - s.x, dy = t.clientY - s.y;
    if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
    sessionStorage.setItem("jbrain_mode", dx > 0 ? "research" : "entry");   // matches Chat's per-session mode
    nav("/chat");
  }

  return (
    <div className="adv-home" onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
      {SECTIONS.map((s) => (
        <div key={s.name}>
          <div className="adv-section">{s.name}</div>
          <div className="adv-grid">
            {s.cards.map((c) => (
              <button key={c.to} className="adv-card" onClick={() => nav(c.to)}>
                <Icon name={c.icon} size={22} />
                <span className="sp" />
                <span className="title">{c.label}</span>
                <span className="sub">{c.sub}</span>
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
