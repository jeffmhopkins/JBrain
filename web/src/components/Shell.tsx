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

// Titles shown in the top bar when a tool is open full-screen (the tool itself
// no longer renders a big heading — the title lives here instead).
const TOOL_TITLES: Record<string, string> = {
  "/wiki": "Wiki",
  "/search": "Search",
  "/graph": "Graph",
  "/prompts": "Prompts",
  "/actions": "Actions",
  "/flows": "Workflows",
  "/sql": "Data",
  "/system": "System",
};

function toolTitle(pathname: string): string {
  if (pathname.startsWith("/note")) return "Note";
  return TOOL_TITLES[pathname] || "Advanced";
}

export default function Shell({ children }: { children: ReactNode }) {
  const online = useOnline();
  const { brainName, versionMismatch, pwaVersion, serverVersion } = useAuth();
  const reviewCount = useReviewCount();
  const loc = useLocation();
  const nav = useNavigate();
  const path = loc.pathname;
  const capture = path === "/chat";
  const review = path === "/review";
  const advHome = path === "/advanced";              // the launcher grid
  const advTool = !capture && !review && !advHome;   // a tool open full-screen
  const advanced = advHome || advTool;

  return (
    <div className="ushell">
      <div className="utop">
        {advTool ? (
          <>
            {/* Back to wherever you came from — the grid, or chat if you deep-linked a note. */}
            <button className="back" title="Back" onClick={() => nav(-1)}><Icon name="chevron" size={20} /></button>
            <span className="tool-title">{toolTitle(path)}</span>
          </>
        ) : (
          <span className="brand">{brainName}<span className="dot">.</span></span>
        )}
        <span className="spacer" />
        {capture && reviewCount > 0 && (
          <button className="bolt review-bell" title={`${reviewCount} to review`} onClick={() => nav("/review")}>
            <Icon name="bell" size={20} />
            <span className="count-badge">{reviewCount}</span>
          </button>
        )}
        {review && <button className="ghost" style={{ padding: "4px 10px" }} onClick={() => nav("/chat")}>Done</button>}
        {!review && (
          <button className={"bolt" + (advanced ? " active" : "")} title={advanced ? "Back to compose" : "Advanced"}
                  onClick={() => nav(advanced ? "/chat" : "/advanced")}>
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
    </div>
  );
}
