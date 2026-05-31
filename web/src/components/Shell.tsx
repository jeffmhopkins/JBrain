import { ReactNode, useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import { useAuth } from "../App";
import { get } from "../api";
import { useIsDesktop, useOnline } from "../hooks";

function useReviewCount(): number {
  const [n, setN] = useState(0);
  useEffect(() => {
    const tick = () => get("/api/reviews/count").then((r) => setN(r.pending)).catch(() => {});
    tick();
    const id = setInterval(tick, 60000);
    return () => clearInterval(id);
  }, []);
  return n;
}

const NAV = [
  { to: "/chat", label: "Chat", ico: "💬" },
  { to: "/wiki", label: "Wiki", ico: "📚" },
  { to: "/graph", label: "Graph", ico: "🕸️" },
  { to: "/search", label: "Search", ico: "🔍" },
  { to: "/flows", label: "Flows", ico: "⚙️" },
  { to: "/sql", label: "SQL", ico: "🗄️" },
];

export default function Shell({ children }: { children: ReactNode }) {
  const isDesktop = useIsDesktop();
  const online = useOnline();
  const { brainName, disconnect } = useAuth();
  const reviewCount = useReviewCount();

  if (isDesktop) {
    return (
      <div className="app">
        <aside className="sidebar">
          <div className="brand">{brainName}<span className="dot">.</span></div>
          <nav className="nav">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? "active" : "")}>
                <span className="ico">{n.ico}</span> {n.label}
              </NavLink>
            ))}
            <NavLink to="/review" className={({ isActive }) => (isActive ? "active" : "")}>
              <span className="ico">🔔</span> Review
              {reviewCount > 0 && <span className="count-badge">{reviewCount}</span>}
            </NavLink>
          </nav>
          <div style={{ marginTop: "auto", paddingTop: 16 }}>
            <button className="ghost" style={{ width: "100%" }} onClick={disconnect}>Disconnect</button>
          </div>
        </aside>
        <div className="main">
          {!online && <div className="offline-banner">Offline — reading cached notes. Chat & saving need a connection.</div>}
          {children}
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <div className="main has-tabbar" style={{ width: "100%" }}>
        <div className="topbar">
          <strong>{brainName}</strong>
          <div className="row" style={{ gap: 8 }}>
            <NavLink to="/review" className="bell">
              🔔{reviewCount > 0 && <span className="count-badge">{reviewCount}</span>}
            </NavLink>
            <button className="ghost" onClick={disconnect}>Disconnect</button>
          </div>
        </div>
        {!online && <div className="offline-banner">Offline — reading cached notes only.</div>}
        {children}
      </div>
      <nav className="tabbar">
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? "active" : "")}>
            <span className="ico">{n.ico}</span>
            <span>{n.label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
