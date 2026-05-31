import { ReactNode, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../App";
import { get, post } from "../api";
import { useOnline } from "../hooks";
import { Icon } from "./Icon";

interface ReviewItem { id: number; title: string; message: string; link_slug: string | null; created_at: string; }

// The review "alerts" bell: a notifications-style dropdown of items (not a whole
// page), so the Advanced bolt stays visible beside it. The bell fills + brightens
// (same treatment as the active bolt) while the menu is open.
function ReviewBell() {
  const [count, setCount] = useState(0);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<ReviewItem[]>([]);
  const ref = useRef<HTMLDivElement>(null);

  const refresh = () => get("/api/reviews/count").then((r) => setCount(r.pending)).catch(() => {});
  useEffect(() => { refresh(); const id = setInterval(refresh, 60000); return () => clearInterval(id); }, []);

  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);

  async function toggle() {
    if (open) { setOpen(false); return; }
    let list: ReviewItem[] = [];
    try { list = await get("/api/reviews"); } catch { /* ignore */ }
    setItems(list);
    setCount(list.length);
    if (list.length) setOpen(true);   // nothing to review → don't show the popup
  }
  async function dismiss(id: number) {
    await post(`/api/reviews/${id}/dismiss`);
    const remaining = items.filter((x) => x.id !== id);
    setItems(remaining);
    setCount(remaining.length);
    if (remaining.length === 0) setOpen(false);   // all cleared → hide the popup
  }

  if (count === 0 && !open) return null;
  return (
    <div className="review-wrap" ref={ref}>
      <button className={"bolt review-bell" + (open ? " active" : "")} title={`${count} to review`} onClick={toggle}>
        <Icon name="bell" size={20} />
        {count > 0 && <span className="count-badge">{count}</span>}
      </button>
      {open && (
        <div className="review-menu">
          <div className="review-menu-head">Review</div>
          {items.map((r) => (
            <div className="review-item" key={r.id}>
              <strong style={{ fontSize: 14 }}>{r.title}</strong>
              {r.message && <div className="muted" style={{ fontSize: 12, margin: "4px 0", whiteSpace: "pre-wrap" }}>{r.message}</div>}
              <div className="row" style={{ gap: 6, marginTop: 4 }}>
                {r.link_slug && <Link className="ghost" style={{ fontSize: 12, padding: "4px 10px" }} to={`/note/${r.link_slug}`} onClick={() => setOpen(false)}>Open</Link>}
                <button className="ghost" style={{ fontSize: 12, padding: "4px 10px" }} onClick={() => dismiss(r.id)}>Dismiss</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
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
  "/flows": "Triggers",
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
        {capture && <ReviewBell />}
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
