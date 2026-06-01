import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

const base = process.env.VITE_BASE || "/";

export default defineConfig({
  // Pages serves a project site at /<repo>/ — set VITE_BASE=/JBrain/ in CI.
  // Same-origin (served by the API) uses "/".
  base,
  define: {
    __PWA_VERSION__: JSON.stringify(process.env.npm_package_version || "0.0.0"),
  },
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      injectRegister: false,   // we register manually (main.tsx) to poll for updates
      includeAssets: ["icon.svg"],
      manifest: {
        name: "JBrain",
        short_name: "JBrain",
        description: "Your self-hosted conversational wiki and thinking partner.",
        theme_color: "#111315",
        background_color: "#111315",
        display: "standalone",
        // Must live within the deploy base (e.g. "/JBrain/") or the installed app
        // launches out of scope and opens in the browser instead of standalone.
        id: base,
        scope: base,
        start_url: base,
        icons: [
          { src: `${base}icon.svg`, sizes: "any", type: "image/svg+xml", purpose: "any maskable" },
        ],
      },
      workbox: {
        // Activate a new version immediately and drop old caches, so a deploy
        // is picked up on the next load rather than after a second visit.
        clientsClaim: true,
        skipWaiting: true,
        cleanupOutdatedCaches: true,
        // Pull our push/notificationclick handlers into the generated SW. Relative
        // specifier => resolves to <base>push-sw.js (correct at "/" and "/JBrain/").
        importScripts: ["push-sw.js"],
        navigateFallback: base + "index.html",
        navigateFallbackDenylist: [/^\/api\//],
        runtimeCaching: [
          {
            // Offline reading: serve last-seen notes/attachments/search offline.
            // Same-origin only — in Pages mode the API is a different origin and
            // its authed responses must not be persisted into the Pages cache.
            // Exclude attachment downloads so large blobs don't fill the cache.
            urlPattern: ({ url }) => url.origin === self.location.origin && (
              url.pathname.startsWith("/api/notes") ||
              url.pathname.startsWith("/api/graph") ||
              url.pathname.startsWith("/api/search") ||
              (url.pathname.startsWith("/api/attachments") && !url.pathname.endsWith("/download"))
            ),
            handler: "NetworkFirst",
            options: {
              cacheName: "jbrain-api",
              networkTimeoutSeconds: 4,
              expiration: { maxEntries: 500, maxAgeSeconds: 60 * 60 * 24 * 30 },
            },
          },
        ],
      },
    }),
  ],
  server: {
    proxy: { "/api": "http://localhost:8000" },
  },
});
