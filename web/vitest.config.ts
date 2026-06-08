import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Separate from vite.config.ts so the PWA/build plugins don't run under tests.
export default defineConfig({
  plugins: [react()],
  define: {
    // Some modules reference this Vite-injected constant at import time; provide a
    // value so those imports don't throw under the test runner.
    __PWA_VERSION__: JSON.stringify(process.env.npm_package_version || "0.0.0-test"),
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
});
