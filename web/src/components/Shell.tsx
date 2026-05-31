import { ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { useAuth } from "../App";
import { useIsDesktop, useOnline } from "../hooks";

const NAV = [
  { to: "/chat", label: "Chat", ico: "💬" },
  { to: "/wiki", label: "Wiki", ico: "📚" },
  { to: "/graph", label: "Graph", ico: "🕸️" },
  { to: "/search", label: "Search", ico: "🔍" },
  { to: "/sql", label: "SQL", ico: "🗄️" },
];

export default function Shell({ children }: { children: ReactNode }) {
  const isDesktop = useIsDesktop();
  const online = useOnline();
  const { brainName, username, logout } = useAuth();

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
          </nav>
          <div style={{ marginTop: "auto", paddingTop: 16 }}>
            <div className="muted" style={{ padding: "0 12px 8px", fontSize: 13 }}>
              {username}
            </div>
            <button className="ghost" style={{ width: "100%" }} onClick={logout}>Log out</button>
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
          <button className="ghost" onClick={logout}>Log out</button>
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
