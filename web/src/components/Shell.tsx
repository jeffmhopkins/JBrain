import { ReactNode, TouchEvent, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../App";
import { get, post } from "../api";
import { enablePush, pushSupported } from "../push";
import { useOnline } from "../hooks";
import { Icon } from "./Icon";

interface ReviewItem { id: number; title: string; message: string; link_slug: string | null; created_at: string; }

// The review "alerts" bell: a notifications-style dropdown of items (not a whole
// page), so the Advanced bolt stays visible beside it. The bell fills + brightens
// (same treatment as the active bolt) while the menu is open.
function ReviewBell() {
  const { vapidPublicKey } = useAuth();
  const [count, setCount] = useState(0);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [pushReady, setPushReady] = useState(false);   // this browser is push-subscribed
  const ref = useRef<HTMLDivElement>(null);

  const refresh = () => get("/api/reviews/count").then((r) => setCount(r.pending)).catch(() => {});

  // Subscribe to push on load if permission is already granted (so a relaunch
  // re-subscribes silently and the bell can drop to the slow poll).
  useEffect(() => {
    if (pushSupported() && "Notification" in window && Notification.permission === "granted") {
      enablePush(vapidPublicKey).then(setPushReady);
    }
  }, [vapidPublicKey]);

  useEffect(() => {
    refresh();
    // When push is the live channel, a much slower poll is enough — it's only a
    // safety net against a silently-dropped push. Otherwise keep the 30s poll.
    // Resume-refresh (visibilitychange/focus/pageshow) always runs: mobile/PWA
    // pause setInterval while backgrounded, so this catches alerts on resume.
    const active = pushSupported() && Notification.permission === "granted" && pushReady;
    const id = setInterval(refresh, active ? 120000 : 30000);
    const onVisible = () => { if (document.visibilityState === "visible") refresh(); };
    // A push wakes any open page so the bell updates live (count rides in the msg).
    const onMsg = (e: MessageEvent) => {
      if (e.data?.type === "jbrain-review") {
        if (typeof e.data.count === "number") setCount(e.data.count); else refresh();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", refresh);
    window.addEventListener("pageshow", refresh);
    navigator.serviceWorker?.addEventListener("message", onMsg);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", refresh);
      window.removeEventListener("pageshow", refresh);
      navigator.serviceWorker?.removeEventListener("message", onMsg);
    };
  }, [pushReady]);

  // Mirror the pending count onto the installed-app icon badge (App Badging API:
  // desktop PWAs, and iOS 16.4+ Home Screen apps once notification permission is
  // granted). No-op where unsupported.
  useEffect(() => {
    const nav = navigator as any;
    if (typeof nav.setAppBadge !== "function") return;
    if (count > 0) nav.setAppBadge(count).catch(() => {});
    else nav.clearAppBadge?.().catch(() => {});
  }, [count]);

  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);

  async function toggle() {
    if (open) { setOpen(false); return; }
    // First time the user engages with alerts, ask for notification permission so
    // the icon badge + push can work (required on installed iOS PWAs). Then
    // subscribe to push and flip off the fast poll. User-gesture only.
    if ("Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch { /* ignore */ }
    }
    if (Notification.permission === "granted") enablePush(vapidPublicKey).then(setPushReady);
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
                {r.link_slug && <Link className="ghost" style={{ fontSize: 12, padding: "4px 10px" }} to={r.link_slug === "__shares__" ? "/shares" : `/note/${r.link_slug}`} onClick={() => setOpen(false)}>Open</Link>}
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
  "/lists": "Lists",
  "/shares": "Shares",
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

// Nearest scrollable ancestor of an element (to gate edge swipes on scroll state).
function scrollParent(el: HTMLElement | null): HTMLElement | null {
  while (el && el !== document.body) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 2) return el;
    el = el.parentElement;
  }
  return null;
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

  // Vertical swipe between chat and Lists. Edge-gated so it never hijacks
  // scrolling: swipe-up only fires at the bottom, swipe-down only at the top.
  //   chat:  ↑ → Lists      (Advanced is on the horizontal carousel instead)
  //   lists: ↓ → chat
  const swipe = useRef<{ y: number; x: number; atTop: boolean; atBottom: boolean } | null>(null);
  function onTouchStart(e: TouchEvent) {
    const t = e.touches[0];
    const sc = scrollParent(e.target as HTMLElement);
    // Slop so "essentially at the edge" counts as the edge. A 2px gate only ever
    // matched the non-scrolling composer (its scroll parent is null → always an
    // edge), so the swipe felt like it worked only from the text field; sub-pixel
    // scroll offsets left the message body just shy of the bottom. 48px matches the
    // chat's own near-bottom threshold so the gesture works from the body too.
    const EDGE = 48;
    const atTop = !sc || sc.scrollTop <= EDGE;
    const atBottom = !sc || sc.scrollHeight - sc.scrollTop - sc.clientHeight <= EDGE;
    swipe.current = { y: t.clientY, x: t.clientX, atTop, atBottom };
  }
  function onTouchEnd(e: TouchEvent) {
    const s = swipe.current; swipe.current = null;
    if (!s) return;
    const t = e.changedTouches[0];
    const dy = t.clientY - s.y, dx = t.clientX - s.x;
    if (Math.abs(dy) < 70 || Math.abs(dy) < Math.abs(dx) * 1.5) return;   // a clear vertical swipe
    const down = dy > 0;
    if (down && !s.atTop) return;       // swipe-down only from the top
    if (!down && !s.atBottom) return;   // swipe-up only from the bottom
    if (path === "/chat") { if (!down) nav("/lists"); }   // swipe up → Lists
    else if (path === "/lists") { if (down) nav("/chat"); }
  }

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
        <ReviewBell />
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

      <div className={"ubody" + (advanced ? " adv" : "")}
           onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>{children}</div>
    </div>
  );
}
