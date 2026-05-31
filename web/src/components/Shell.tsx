import { ReactNode, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../App";
import { get, post } from "../api";
import { useOnline } from "../hooks";
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
  useEffect(() => { get("/api/system/version").then(setInfo).catch(() => {}); }, []);
  async function doUpdate() {
    setMsg("Requesting update…");
    const r = await post("/api/system/update");
    setMsg(r.message || (r.started ? "Updating…" : "Update requested."));
  }
  if (!info?.update_available) return null;
  return (
    <div className="update-banner">
      {msg ? <span>{msg}</span> : (
        <>
          <span>Update available: {info.current} → {info.latest}</span>
          {info.release_url && <a href={info.release_url} target="_blank" rel="noreferrer">notes</a>}
          <button className="primary" style={{ padding: "4px 12px" }} onClick={doUpdate}>Update</button>
        </>
      )}
    </div>
  );
}

const GROUPS = [
  { to: "/browse", label: "Browse", icon: "wiki", match: ["/browse", "/wiki", "/graph", "/search", "/note"] },
  { to: "/flows", label: "Automate", icon: "flows", match: ["/flows"] },
  { to: "/sql", label: "Data", icon: "sql", match: ["/sql"] },
];

export default function Shell({ children }: { children: ReactNode }) {
  const online = useOnline();
  const { brainName, disconnect, versionMismatch, pwaVersion, serverVersion } = useAuth();
  const reviewCount = useReviewCount();
  const loc = useLocation();
  const nav = useNavigate();
  const capture = loc.pathname === "/chat";
  const review = loc.pathname === "/review";
  const advanced = !capture && !review;  // the grouped section pages

  return (
    <div className="ushell">
      <div className="utop">
        <span className="brand">{brainName}<span className="dot">.</span></span>
        <span className="spacer" />
        {advanced && <button className="ghost" style={{ padding: "4px 10px" }} onClick={disconnect}>Disconnect</button>}
        {capture && reviewCount > 0 && (
          <button className="bolt review-bell" title={`${reviewCount} to review`} onClick={() => nav("/review")}>
            <Icon name="bell" size={20} />
            <span className="count-badge">{reviewCount}</span>
          </button>
        )}
        {review && <button className="ghost" style={{ padding: "4px 10px" }} onClick={() => nav("/chat")}>Done</button>}
        {!review && (
          <button className={"bolt" + (advanced ? " active" : "")} title="Advanced"
                  onClick={() => nav(advanced ? "/chat" : "/browse")}>
            <Icon name="bolt" size={20} />
          </button>
        )}
      </div>

      <UpdateBanner />
      {versionMismatch && (
        <div className="version-banner">App v{pwaVersion} vs server v{serverVersion} — versions differ; update so they match.</div>
      )}
      {!online && <div className="offline-banner">Offline — reading cached notes only.</div>}

      <div className={"ubody" + (advanced ? " adv" : "")}>{children}</div>

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
