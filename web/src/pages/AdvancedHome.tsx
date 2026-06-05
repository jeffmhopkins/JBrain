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
      { to: "/entities", label: "Entities", sub: "People, orgs & things in your notes", icon: "entities" },
      { to: "/map", label: "Map", sub: "Location trail & heatmap", icon: "pin" },
      { to: "/people", label: "Users", sub: "Trail attribution & colours", icon: "people" },
      { to: "/medical", label: "Medical", sub: "Log destinations", icon: "medical" },
      { to: "/labs", label: "Labs", sub: "Result trends", icon: "graph" },
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

  // Swipe-down → Chat is handled by the shell body; the cards just navigate on tap.
  return (
    <div className="adv-home">
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
