import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { registerSW } from "virtual:pwa-register";
import App from "./App";
import "./styles.css";

// Network-first updates: register immediately, auto-apply new versions
// (registerType: autoUpdate), and poll the network hourly so long-open
// sessions pick up a new deploy.
registerSW({
  immediate: true,
  onRegisteredSW(_swUrl, r) {
    if (r) setInterval(() => r.update(), 60 * 60 * 1000);
  },
});

// A new SW (skipWaiting + clientsClaim) activates and claims open tabs, but the
// page keeps the OLD JS until reloaded. Reload once when control changes so an
// open/installed PWA actually gets the new bundle (no manual hard-refresh). Only
// when there was already a controller — i.e. an update, not the first install.
if (navigator.serviceWorker?.controller) {
  let reloaded = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (reloaded) return;
    reloaded = true;
    window.location.reload();
  });
}

// Don't pester share-link recipients to install the PWA. The public
// /share/:token page is a one-off view for someone who isn't the owner, so
// swallow the browser's install prompt there (Chrome/Edge fire
// `beforeinstallprompt`; calling preventDefault suppresses the mini-infobar).
// Strip the deploy base so this holds at "/" and under a sub-path (e.g.
// "/JBrain/") alike.
window.addEventListener("beforeinstallprompt", (e) => {
  const base = import.meta.env.BASE_URL.replace(/\/$/, "");
  const path = window.location.pathname.startsWith(base)
    ? window.location.pathname.slice(base.length)
    : window.location.pathname;
  if (path.startsWith("/share/")) e.preventDefault();
});

// Point the favicon at the deploy base (e.g. "/JBrain/icon.svg" on Pages); the
// static index.html link is "/icon.svg", which 404s under a sub-path base.
const fav = document.querySelector<HTMLLinkElement>("link[rel~='icon']");
if (fav) fav.href = import.meta.env.BASE_URL + "icon.svg";

// Keep the shell sized to the actual visible viewport (handles the mobile
// keyboard and toolbars, which shrink the visual viewport but not the layout).
function syncAppHeight() {
  const h = window.visualViewport?.height ?? window.innerHeight;
  document.documentElement.style.setProperty("--app-height", `${Math.round(h)}px`);
}
syncAppHeight();
window.visualViewport?.addEventListener("resize", syncAppHeight);
window.visualViewport?.addEventListener("scroll", syncAppHeight);
window.addEventListener("resize", syncAppHeight);
window.addEventListener("orientationchange", syncAppHeight);

// Route under the deploy base path (e.g. "/JBrain/" on GitHub Pages, "/" when
// the API serves the app) so links/refreshes keep the correct prefix.
const basename = import.meta.env.BASE_URL.replace(/\/$/, "") || "/";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter basename={basename}>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
