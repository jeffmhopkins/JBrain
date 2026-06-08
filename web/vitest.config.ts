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
    restoreMocks: true, // auto vi.restoreAllMocks() between tests
    unstubGlobals: true, // auto vi.unstubAllGlobals() between tests
    unstubEnvs: true,
    coverage: {
      provider: "v8", // @vitest/coverage-v8 (pinned to the vitest version)
      reporter: ["text-summary", "html", "lcov"],
      reportsDirectory: "./coverage",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.{test,spec}.{ts,tsx}",
        "src/test/**",
        "src/main.tsx", // bootstrap; exercised by e2e, not units
        "src/**/*.d.ts",
      ],
      // FLOOR, not a target — pinned just under today's ~7.8% so CI is green on day
      // one and can only ratchet UP. Raise as feature components get covered.
      thresholds: { lines: 40, functions: 40, statements: 40, branches: 40 },
    },
  },
});
