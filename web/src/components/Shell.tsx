import { ReactNode, TouchEvent, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../App";
import { get, post } from "../api";
import { enablePush, pushSupported } from "../push";
import { useLocationTrail, useOnline } from "../hooks";
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
                {r.link_slug && <Link className="ghost" style={{ fontSize: 12, padding: "4px 10px" }} to={r.link_slug.startsWith("/") ? r.link_slug : r.link_slug === "__shares__" ? "/shares" : `/note/${r.link_slug}`} onClick={() => setOpen(false)}>Open</Link>}
                <button className="ghost" style={{ fontSize: 12, padding: "4px 10px" }} onClick={() => dismiss(r.id)}>Dismiss</button>
              </div>
            </div>
          ))}
        </div>
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
  "/map": "Map",
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
  useLocationTrail();   // foreground location trail while the app is open (opt-in via location toggle)
  const { brainName, versionMismatch, pwaVersion, serverVersion } = useAuth();
  const loc = useLocation();
  const nav = useNavigate();
  const path = loc.pathname;
  const capture = path === "/chat";
  const review = path === "/review";
  const advHome = path === "/advanced";              // the launcher grid
  const advTool = !capture && !review && !advHome;   // a tool open full-screen
  const advanced = advHome || advTool;

  // Swipes anchored on the composer text box (gating on the input, not the scroll
  // position, means scrolling the message body never navigates — only a deliberate
  // swipe from the box does):
  //   chat:  ↑ from the text box → Lists;  ← → Search;  → → Wiki.
  //   lists: ↓ from the top → chat.
  const swipe = useRef<{ y: number; x: number; fromComposer: boolean; atTop: boolean; edgeStart: boolean } | null>(null);
  function onTouchStart(e: TouchEvent) {
    const t = e.touches[0];
    const el = e.target as HTMLElement;
    const sc = scrollParent(el);
    swipe.current = {
      y: t.clientY, x: t.clientX,
      fromComposer: !!el.closest(".composer-box"),
      atTop: !sc || sc.scrollTop <= 2,
      // A horizontal swipe that STARTS in the OS edge gutter is the system back/
      // forward gesture (Android uses both edges). The composer spans the full
      // width at the bottom, so without this an edge-back lands on it and our
      // nav() fires too — fighting the system back and dumping out of the PWA.
      edgeStart: t.clientX <= 30 || t.clientX >= window.innerWidth - 30,
    };
  }
  function onTouchEnd(e: TouchEvent) {
    const s = swipe.current; swipe.current = null;
    if (!s) return;
    const t = e.changedTouches[0];
    const dy = t.clientY - s.y, dx = t.clientX - s.x;
    // Horizontal swipe from the text box: ← → Search, → → Wiki (deliberate swipe,
    // started clear of the OS edge gutter so it can't collide with system back).
    if (Math.abs(dx) >= 70 && Math.abs(dx) > Math.abs(dy) * 1.5) {
      if (s.fromComposer && !s.edgeStart) nav(dx < 0 ? "/search" : "/wiki");
      return;
    }
    if (Math.abs(dy) < 70 || Math.abs(dy) < Math.abs(dx) * 1.5) return;   // a clear vertical swipe
    const down = dy > 0;
    if (path === "/chat") { if (!down && s.fromComposer) nav("/lists"); }   // swipe up from the text box → Lists
    else if (path === "/lists") { if (down && s.atTop) nav("/chat"); }
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

      {versionMismatch && (
        <div className="version-banner">App v{pwaVersion} vs server v{serverVersion} — versions differ; update from System.</div>
      )}
      {!online && <div className="offline-banner">Offline — reading cached notes only.</div>}

      <div className={"ubody" + (advanced ? " adv" : "")}
           onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>{children}</div>
    </div>
  );
}
