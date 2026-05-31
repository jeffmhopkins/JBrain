import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { registerSW } from "virtual:pwa-register";
import App from "./App";
import "./styles.css";

// Network-first updates: register immediately, auto-apply new versions
// (registerType: autoUpdate), and poll the network hourly so long-open
// sessions refresh their cache too.
registerSW({
  immediate: true,
  onRegisteredSW(_swUrl, r) {
    if (r) setInterval(() => r.update(), 60 * 60 * 1000);
  },
});

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
