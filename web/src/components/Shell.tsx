import { ReactNode, useEffect, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../App";
import { get, post } from "../api";
import { useIsDesktop, useOnline } from "../hooks";
import { Icon } from "./Icon";

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

function UpdateBanner() {
  const [info, setInfo] = useState<any>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    get("/api/system/version").then(setInfo).catch(() => {});
  }, []);

  async function doUpdate() {
    setMsg("Requesting update…");
    const r = await post("/api/system/update");
    setMsg(r.message || (r.started ? "Updating… the server will restart shortly." : "Update requested."));
  }

  if (!info?.update_available) return null;
  return (
    <div className="update-banner">
      {msg ? (
        <span>{msg}</span>
      ) : (
        <>
          <span>Update available: {info.current} → {info.latest}</span>
          {info.release_url && <a href={info.release_url} target="_blank" rel="noreferrer">notes</a>}
          <button className="primary" style={{ padding: "4px 12px" }} onClick={doUpdate}>Update</button>
        </>
      )}
    </div>
  );
}

const NAV = [
  { to: "/chat", label: "Chat", icon: "chat" },
  { to: "/wiki", label: "Wiki", icon: "wiki" },
  { to: "/graph", label: "Graph", icon: "graph" },
  { to: "/search", label: "Search", icon: "search" },
  { to: "/flows", label: "Flows", icon: "flows" },
  { to: "/sql", label: "SQL", icon: "sql" },
];

export default function Shell({ children }: { children: ReactNode }) {
  const isDesktop = useIsDesktop();
  const online = useOnline();
  const { brainName, disconnect, versionMismatch, pwaVersion, serverVersion, demo } = useAuth();
  const reviewCount = useReviewCount();

  const demoBanner = demo ? (
    <div className="demo-banner">Demo mode — sample data, no server. “Disconnect” to exit.</div>
  ) : null;

  const versionWarning = versionMismatch ? (
    <div className="version-banner">
      App v{pwaVersion} vs server v{serverVersion} — versions differ; update so they match.
    </div>
  ) : null;

  if (isDesktop) {
    return (
      <div className="app">
        <aside className="sidebar">
          <div className="brand">{brainName}<span className="dot">.</span></div>
          <nav className="nav">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? "active" : "")}>
                <Icon name={n.icon} /> {n.label}
              </NavLink>
            ))}
            <NavLink to="/review" className={({ isActive }) => (isActive ? "active" : "")}>
              <Icon name="bell" /> Review
              {reviewCount > 0 && <span className="count-badge">{reviewCount}</span>}
            </NavLink>
          </nav>
          <div style={{ marginTop: "auto", paddingTop: 16 }}>
            <button className="ghost" style={{ width: "100%" }} onClick={disconnect}>Disconnect</button>
          </div>
        </aside>
        <div className="main">
          {demoBanner}
          <UpdateBanner />
          {versionWarning}
          {!online && <div className="offline-banner">Offline — reading cached notes. Chat & saving need a connection.</div>}
          {children}
        </div>
      </div>
    );
  }

  return <MobileShell {...{ children, brainName, disconnect, reviewCount, online, demoBanner, versionWarning }} />;
}

const CAPTURE = [
  { key: "entry", label: "Entry" },
  { key: "assisted", label: "Assisted" },
  { key: "research", label: "Research" },
];
const GROUPS = [
  { to: "/browse", label: "Browse", icon: "wiki", match: ["/browse", "/wiki", "/graph", "/search", "/note"] },
  { to: "/flows", label: "Automate", icon: "flows", match: ["/flows"] },
  { to: "/sql", label: "Data", icon: "sql", match: ["/sql"] },
  { to: "/review", label: "Review", icon: "bell", match: ["/review"] },
];

function MobileShell({ children, brainName, disconnect, reviewCount, online, demoBanner, versionWarning }: any) {
  const loc = useLocation();
  const nav = useNavigate();
  const mode = new URLSearchParams(loc.search).get("m") || "entry";
  const advanced = loc.pathname !== "/chat";

  return (
    <div className="mshell">
      <div className="mtop">
        {CAPTURE.map((c) => (
          <button key={c.key} className={"mtab" + (!advanced && mode === c.key ? " active" : "")}
                  onClick={() => nav(`/chat?m=${c.key}`)}>{c.label}</button>
        ))}
        <button className={"mtab" + (advanced ? " active" : "")} onClick={() => nav("/browse")}>Advanced</button>
      </div>

      {demoBanner}
      <UpdateBanner />
      {versionWarning}
      {!online && <div className="offline-banner">Offline — reading cached notes only.</div>}

      {advanced && (
        <div className="madv-head">
          <span className="brand">{brainName}<span className="dot">.</span></span>
          <span className="spacer" />
          <button className="ghost" onClick={disconnect}>Disconnect</button>
        </div>
      )}

      <div className={"mbody" + (advanced ? " adv" : "")}>{children}</div>

      {advanced && (
        <nav className="mbottom">
          {GROUPS.map((g) => {
            const active = g.match.some((p) => loc.pathname.startsWith(p));
            return (
              <button key={g.to} className={active ? "active" : ""} onClick={() => nav(g.to)}>
                <Icon name={g.icon} size={20} />
                <span>{g.label}</span>
                {g.label === "Review" && reviewCount > 0 && <span className="count-badge">{reviewCount}</span>}
              </button>
            );
          })}
        </nav>
      )}
    </div>
  );
}
