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
        theme_color: "#0f172a",
        background_color: "#0f172a",
        display: "standalone",
        start_url: "/",
        icons: [
          { src: "/icon.svg", sizes: "any", type: "image/svg+xml", purpose: "any maskable" },
        ],
      },
      workbox: {
        // Activate a new version immediately and drop old caches, so a deploy
        // is picked up on the next load rather than after a second visit.
        clientsClaim: true,
        skipWaiting: true,
        cleanupOutdatedCaches: true,
        navigateFallback: base + "index.html",
        navigateFallbackDenylist: [/^\/api\//],
        runtimeCaching: [
          {
            // Offline reading: serve last-seen notes/attachments/search offline.
            // Exclude attachment downloads so large blobs don't fill the cache.
            urlPattern: ({ url }) => (
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
