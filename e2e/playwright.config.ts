import { defineConfig, devices } from "@playwright/test";

// System / E2E tier: drive the REAL built PWA in a real browser against a REAL
// FastAPI server, with the LLM mocked at the boundary (XAI_BASE_URL -> fake_llm.py).
// Two webServers are booted: the fake LLM, then the JBrain API (which serves the PWA
// from static/). See docs/testing-plan/PLAN.md §6 and run-jbrain.sh.

const API_PORT = 8918;
const LLM_PORT = 8919;
export const ACCESS_KEY = "e2e-access-key-0123456789abcdef";

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.system.spec.ts",
  fullyParallel: false, // shared single server + SQLite — keep specs serial
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${API_PORT}`,
    trace: "on-first-retry",
    headless: true,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `python -m uvicorn --app-dir . fake_llm:app --host 127.0.0.1 --port ${LLM_PORT}`,
      url: `http://127.0.0.1:${LLM_PORT}/healthz`,
      reuseExistingServer: !process.env.CI,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      command: `bash run-jbrain.sh`,
      env: {
        JBRAIN_PORT: String(API_PORT),
        XAI_BASE_URL: `http://127.0.0.1:${LLM_PORT}/v1`,
        JBRAIN_ACCESS_KEY: ACCESS_KEY,
      },
      url: `http://127.0.0.1:${API_PORT}/api/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      stdout: "ignore",
      stderr: "pipe",
    },
  ],
});
