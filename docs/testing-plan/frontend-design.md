# JBrain Frontend Testing Standard

**Scope:** `web/` — the React 18 + Vite PWA (~16k LOC, 26 pages, 25+ components).
**Runner:** vitest 2.1.9 + jsdom + React Testing Library (already installed).
**Status quo:** 10 test files / ~72 tests, ~7.8% line coverage; every feature page/component at 0%.
**Audience:** a common cross-domain test framework (the `/test` entrypoint) that needs a stable, opinionated frontend contract to plug into.

This document is a **standard**, not a migration PR. It defines tiers, layout, mocking, shared utilities, config, determinism rules, the run contract, and the migration cost. It deliberately mirrors the backend's tier spirit (unit / integration / system) so a single orchestrator can reason about both stacks uniformly.

---

## 1. Test taxonomy

Three tiers, named to align in spirit with the backend (unit / API-integration / system-E2E). The dividing line is **what is real vs mocked** and **what boundary is crossed**.

### Tier 1 — Unit (pure logic)
- **What qualifies:** a single module of pure or near-pure logic, no DOM, no network, no React render. Functions and reducers: `util.ts`, `time.ts`, `diff.ts`, `statusDerive.ts`, `toolHistoryParse.ts`, `toolLabels.ts`, `swipeGesture.ts`, the `crypto.ts` primitives, the pure exports of `health.ts` (`pollDelay`, `compute` via the store API), `capabilities.ts` copy tables, and `observedFeed`/`toast` reducers.
- **Realness:** everything in-process; no `fetch`, no timers needed (or trivially faked).
- **Cost:** cheapest, fastest, highest signal-per-line. Most new coverage should be pushed *down* into this tier by extracting logic out of components where practical (audit P2 item 10).
- **Existing examples:** `capabilities.test.ts`, `statusDerive.test.ts`, `observedFeed.test.ts`, `toast.test.ts`, `health.test.ts`, the `pollDelay` block of `healthPoll.test.ts`.

### Tier 2 — Component-integration (render + interaction, mocked network)
- **What qualifies:** render a real component or page through React Testing Library in jsdom, drive it with `user-event`/`fireEvent`, and assert on the rendered DOM and on the **outgoing API calls** (which are mocked). This is the analogue of the backend's in-process API-integration tests (real app, real router, mocked LLM/embeddings/DB-at-boundary).
- **Realness:** real component tree, real `react-router`, real health store, real `api.ts` client code — but the **network boundary is mocked** (see §3). Web Crypto, geolocation, Notification, timers are deterministic (see §6).
- **Cost:** moderate. This is where the bulk of feature-coverage gains live (`Chat`, `NotePage`, `SharesPage`, `CalendarPage`, `RebuildPanel`, …).
- **Existing examples:** `SearchPageGating.test.tsx` (renders `SearchPage` in a `MemoryRouter`, asserts which `/api/search` URLs fire), `StatusDot.test.tsx`, `Toaster.test.tsx`.

### Tier 3 — System / E2E (real browser, real stack)
- **What qualifies:** a real browser driving the **built** PWA against a **real running FastAPI server** (LLM/embeddings mocked only at the server's external boundary). Covers the PWA↔API contract, the service-worker/PWA shell, the encrypted-chat crypto round-trip across two browser contexts, and the canonical user flows from the audit (capture→Wiki, Full-Brain→Apply, share→encrypted chat).
- **Realness:** everything real except third-party LLM calls.
- **Tooling decision:** **Tier 3 is Playwright and lives OUTSIDE vitest** — it is a cross-domain concern (it needs a built front end *and* a live backend) and belongs in the shared cross-domain framework, not in `web/vitest.config.ts`. There is currently **no** Playwright; standing it up is audit item P3 and is explicitly out of scope for this frontend-vitest standard. We note the seam here so the cross-domain `/test full` can later add a `web/e2e/` Playwright project without disturbing tiers 1–2.

**Recommendation:** component-integration (Tier 2) stays in **vitest + jsdom**. True browser E2E (Tier 3) is **separate (Playwright)**. Do not try to do real-browser E2E in jsdom (no service worker, no real fetch, no second browser context), and do not push component-integration into Playwright (10–100x slower, needs a live server). The two layers have complementary failure modes.

---

## 2. Directory & naming conventions

### Colocated, not `__tests__/`
**Keep tests colocated next to source** as `Name.test.ts(x)` — this matches the current repo and is the right call:

- The `include` glob is already `src/**/*.{test,spec}.{ts,tsx}`, so colocation needs zero config change.
- A component and its test move/rename/delete together; reviewers see the test in the same diff hunk.
- Coverage exclusion of test files is trivial (`**/*.test.*`).

**One refinement:** colocate inside the source's own folder, not at `src/` root. Today component tests sit at `src/StatusDot.test.tsx` / `src/Toaster.test.tsx` / `src/SearchPageGating.test.tsx` while their sources live in `src/components/` and `src/pages/`. New tests MUST sit beside their subject:

```
web/src/components/StatusDot.tsx        web/src/components/StatusDot.test.tsx
web/src/pages/SearchPage.tsx            web/src/pages/SearchPage.test.tsx
web/src/util.ts                         web/src/util.test.ts
```

The three root-level component tests should be relocated next to their sources during migration (§8). `*.spec.*` is permitted by the glob but **prefer `*.test.*`** for consistency (the whole repo uses `.test.`).

### Naming of `describe` / `it`
- **`describe`** names the unit under test: the exported symbol, hook, or component — `describe("getStatus", …)`, `describe("useCapability", …)`, `describe("Toaster", …)`. For a sub-behaviour group, a second nested `describe` (e.g. `describe("CAP_COPY exhaustiveness")`).
- **`it`** states an observable behaviour as a sentence completing "it …", in plain language describing the *user-* or *caller-visible* outcome, not the implementation — current files do this well: `it("falls back to hybrid (never fires a semantic request) while embeddings warm")`, `it("maps a 5xx to unreachable (does not throw)")`. Avoid `it("works")` / `it("test 1")`.
- Tie a test to a tracked requirement with a short tag in the title or a one-line comment when relevant (the codebase already does, e.g. `(R3-M4)`).

---

## 3. API mocking strategy

### Decision: **MSW (Mock Service Worker) for component-integration; keep tiny hand-rolled stubs only for unit-level api-client tests.**

**Why MSW for Tier 2.** The app's network surface is large and varied: `api.ts` alone has ~120 endpoints plus three transports that are NOT plain `fetch`+JSON — SSE streams (`streamChat`, `openChatStream`, `streamSSE`/rebuild), `XMLHttpRequest` uploads (`uploadAttachment`), multipart `FormData`, blob/arraybuffer downloads, and `publicApi` (cookie-auth, no bearer). Hand-rolling `vi.stubGlobal("fetch", …)` per test (as `SearchPageGating.test.tsx` does) forces every test to re-encode URL matching, status, and body shape — brittle and duplicated across dozens of future page tests. MSW lets each test **declare the server contract by route** in one shared default handler set, override per-test, and assert that unhandled requests fail loudly (so a page silently hitting an un-mocked endpoint is caught, not papered over). MSW intercepts at the `fetch`/XHR layer in node, so the **real `api.ts` code runs** (auth header injection, `ApiError` categorization, `report()` health wiring) — that is exactly the integration value we want.

**Why NOT only hand-rolled mocks.** Hand-rolled `fetch` stubs are fine for a 3-assertion api-client unit test but do not scale to page tests and cannot model cookie-auth, streaming bodies, or "fail on unexpected request" without a lot of bespoke code.

**Where hand-rolled stays.** The existing `api.getStatus.test.ts` style — `vi.stubGlobal("fetch", …)` to assert client-level behaviour (timeout/abort, 5xx→unreachable mapping, bearer header) — is the **right** tool for Tier 1 api-client tests and should remain. Those tests assert the client's *handling* of raw responses, where a literal `Response` object is clearer than a route handler. SSE handlers (`streamChat`) are also easier to unit-test with a hand-rolled `ReadableStream`-backed `fetch` stub than via MSW.

**Add the dependency:** `msw` (`^2`) to `web/devDependencies`. Wire the node server in shared setup (§4/§5).

### Canonical MSW pattern (Tier 2)

```ts
// web/src/test/handlers.ts — the default "happy" contract, overridable per test.
import { http, HttpResponse } from "msw";

const FULL_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: false } },
  embeddings: { state: "ready" }, transcription: { state: "ready" },
  push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
};

export const handlers = [
  http.get("*/api/system/status", () => HttpResponse.json({ ok: true, capabilities: FULL_CAPS })),
  http.get("*/api/auth/info", () => HttpResponse.json({ brain_name: "Test Brain" })),
  http.get("*/api/auth/verify", () => HttpResponse.json({ version: "test", has_llm: true, owner_set: true, capabilities: FULL_CAPS })),
  http.get("*/api/search", () => HttpResponse.json([])),
  // …one happy-path handler per endpoint family the suite touches.
];
```

```ts
// web/src/test/server.ts
import { setupServer } from "msw/node";
import { handlers } from "./handlers";
export const server = setupServer(...handlers);
```

```ts
// In a page test — override just the route this case cares about, and assert the request.
import { http, HttpResponse } from "msw";
import { server } from "../test/server";

it("posts the entry text and shows a success toast", async () => {
  let captured: any;
  server.use(
    http.post("*/api/notes/entry", async ({ request }) => {
      captured = await request.json();
      return HttpResponse.json({ note_id: 1, note_title: "Note" });
    }),
  );
  renderWithProviders(<Chat />, { route: "/chat" });
  await userEvent.type(screen.getByRole("textbox"), "buy milk");
  await userEvent.click(screen.getByRole("button", { name: /capture/i }));
  expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  expect(captured).toMatchObject({ text: "buy milk" });
});
```

`server.onUnhandledRequest` is set to `"error"` (see setup) so any un-mocked call fails the test — no accidental real network, no silent gaps. Wildcard (`*/api/...`) prefixes match both same-origin and a configured `serverBase`.

---

## 4. Shared test-utils

All shared helpers live under **`web/src/test/`** (next to the existing `setup.ts`). Nothing app-importable; test-only.

```
web/src/test/
  setup.ts          # global setup (jest-dom, MSW lifecycle, deterministic globals)
  server.ts         # MSW node server
  handlers.ts       # default happy-path route handlers
  render.tsx        # renderWithProviders + re-exports
  mocks/
    crypto.ts       # (optional) WebCrypto helpers if a real impl is unavailable
```

### `renderWithProviders`
A single render helper that wraps the subject in the providers the app needs (router + the auth context + the toast host), so page tests stop re-stamping `<MemoryRouter>` boilerplate. It also returns a configured `user-event` instance.

```tsx
// web/src/test/render.tsx
import { render, type RenderOptions } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement, ReactNode } from "react";

interface Opts extends Omit<RenderOptions, "wrapper"> {
  route?: string;            // initial URL, e.g. "/search?q=hi"
  // future: auth?: Partial<AuthState> to override the AuthCtx provider value
}

export function renderWithProviders(ui: ReactElement, { route = "/", ...rest }: Opts = {}) {
  function Wrapper({ children }: { children: ReactNode }) {
    return <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>;
  }
  return {
    user: userEvent.setup(),   // NB: pair with vi.useFakeTimers({ shouldAdvanceTime }) — see §6
    ...render(ui, { wrapper: Wrapper, ...rest }),
  };
}

// Re-export RTL so tests import everything from one place.
export * from "@testing-library/react";
export { userEvent };
```

> The `AuthCtx` provider is exported from `App.tsx` via `useAuth`; once a test needs an authed page, extend `renderWithProviders` to accept an `auth` override and wrap in a real `<AuthCtx.Provider>` with a default fixture. Today component tests `vi.mock("./App", () => ({ useAuth: () => ({ brainName: "Test Brain" }) }))` — that pattern stays valid but the provider approach is preferred for pages that read more of the auth state.

### Common mocks (centralized in `setup.ts`)
- **Network:** MSW server (`listen` / `resetHandlers` / `close`) — the *only* sanctioned network mock for Tier 2.
- **`localStorage`/`sessionStorage`:** jsdom provides both; reset between tests (`localStorage.clear()` in an `afterEach`) because `api.ts` reads `jbrain_access_key` / `jbrain_server` at import time and `setAccessKey`/`setServer` write to it.
- **Health store:** call `__reset()` from `health.ts` in `beforeEach` (already used everywhere) to clear the module singleton.
- **Toasts:** `__resetToasts()` from `toast.ts`.
- **Web Crypto / geolocation / Notification:** see §6.

---

## 5. Config

### `web/vitest.config.ts` — exact additions
Add `setupFiles` stays the same path; add a coverage block and an explicit `exclude`. Full target file:

```ts
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Separate from vite.config.ts so the PWA/build plugins don't run under tests.
export default defineConfig({
  plugins: [react()],
  define: {
    __PWA_VERSION__: JSON.stringify(process.env.npm_package_version || "0.0.0-test"),
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    restoreMocks: true,        // auto vi.restoreAllMocks() between tests
    unstubGlobals: true,       // auto vi.unstubAllGlobals() between tests
    unstubEnvs: true,
    coverage: {
      provider: "v8",          // requires @vitest/coverage-v8 (commit it to devDeps)
      reporter: ["text-summary", "html", "json", "lcov"],
      reportsDirectory: "./coverage",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.{test,spec}.{ts,tsx}",
        "src/test/**",
        "src/main.tsx",          // bootstrap, exercised by E2E not units
        "src/global.d.ts",
        "src/**/*.d.ts",
      ],
      thresholds: {
        // FLOOR, not a target. Web is ~7.8% today; set the floor just under that so CI
        // is green on day one and can only RATCHET UP. Raise these as tiers fill in.
        lines: 7,
        functions: 7,
        statements: 7,
        branches: 7,
        // After the first 5 feature components land, bump to ~25 and ratchet from there.
      },
    },
  },
});
```

### `web/src/test/setup.ts` — exact target
```ts
import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll } from "vitest";
import { server } from "./server";
import { __reset } from "../health";
import { __resetToasts } from "../toast";

// --- MSW: no real network; unhandled requests are an error, not a silent miss. ---
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();   // drop per-test http.* overrides
  __reset();                // clear the health-store singleton
  __resetToasts();
  localStorage.clear();
  sessionStorage.clear();
});
afterAll(() => server.close());
```

### `web/package.json` — scripts
```json
"scripts": {
  "dev": "vite",
  "build": "tsc -b && vite build",
  "preview": "vite preview",
  "test": "vitest run",
  "test:watch": "vitest",
  "test:cov": "vitest run --coverage",
  "test:ui": "vitest --ui"
}
```
Add to `devDependencies`: `"@vitest/coverage-v8": "^2.1.9"` (pin to the vitest version) and `"msw": "^2.7.0"`. `@vitest/coverage-v8` is currently installed locally but **uncommitted** — committing it is mandatory (audit P0).

---

## 6. Determinism rules

No test may depend on wall-clock time, real randomness, real network, real device APIs, or test-ordering. Concretely:

1. **No real network — ever.** MSW with `onUnhandledRequest: "error"` enforces this for Tier 2; Tier 1 api-client tests use `vi.stubGlobal("fetch", …)`. A test that needs the network is a Tier 3 (Playwright) test and does not belong here.
2. **Fake timers for anything time-driven.** `health.ts` polling, the 200ms search debounce, the 8s `getStatus` abort, the 90s SSE stall watchdog: use `vi.useFakeTimers()` + `await vi.advanceTimersByTimeAsync(ms)` (the codebase already does this correctly in `SearchPageGating`/`healthPoll`/`api.getStatus`). Always restore with `vi.useRealTimers()` in `afterEach`. When combining fake timers with `userEvent`, create the user with `userEvent.setup({ advanceTimers: vi.advanceTimersByTime })` so typing doesn't hang. Because `vi.useFakeTimers()` also mocks `Date.now()`, the health store's `_now()` is automatically deterministic.
3. **Web Crypto must be deterministic.** `crypto.ts` uses `crypto.getRandomValues` + `crypto.subtle` (PBKDF2/AES-GCM). jsdom in Node ≥18 exposes `globalThis.crypto.subtle`, so the real implementation runs — prefer that for `crypto.test.ts` (real round-trip: encrypt→decrypt). When a test asserts *exact* ciphertext/wrapped bytes, stub `crypto.getRandomValues` to a fixed sequence via `vi.spyOn(globalThis.crypto, "getRandomValues")`. Never assert on un-seeded random output.
4. **Geolocation, Notification, service worker, PushManager:** mock as globals per test (the app feature-detects all of them — `pushSupportReason()` checks `"serviceWorker" in navigator`, `"PushManager" in window`, `"Notification" in window`). Default: leave them undefined (so push/geo code takes the unsupported no-op branch). To test the supported path, `vi.stubGlobal("Notification", { permission: "granted", requestPermission: vi.fn() })`, stub `navigator.geolocation.getCurrentPosition` to call back with a fixed `{ coords: { latitude, longitude } }`, etc.
5. **No shared mutable state across tests.** The module-singleton stores (`health.ts`, `toast.ts`) are reset in `setup.ts`'s `afterEach`; `localStorage`/`sessionStorage` cleared there too. `restoreMocks`/`unstubGlobals` in config undo `vi.fn`/`vi.stubGlobal` automatically.
6. **Stable DOM queries.** Prefer role/label/text queries (`getByRole`, `getByLabelText`, `findByText`) over CSS-class selectors. The existing `StatusDot` test reaches into `.status-dot` classes — acceptable for a tightly-styled status dot, but new tests should query by accessible role/name first (this also nudges accessibility).
7. **`act()` around store mutations and timer advances** that trigger React re-renders (as the current tests do). Use `findBy*` (async) over `getBy*`+manual flush where possible.

---

## 7. What the single `test` entrypoint must call

The cross-domain `/test` orchestrator runs frontend checks by invoking these **exact** commands from the `web/` directory:

**Unit-only (fast inner loop / pre-commit):**
```bash
npm --prefix web run test
# == vitest run  (Tiers 1 + 2, no coverage; jsdom; ~seconds)
```
Vitest tier separation is by file content, not separate configs, so `vitest run` executes both unit and component-integration tiers. To run *only* pure-unit files when speed matters, filter by path:
```bash
npm --prefix web run test -- --exclude 'src/**/*.test.tsx'   # .ts only ⇒ no React renders
```

**Full (CI / `/test full`):**
```bash
npm --prefix web ci            # reproducible install from package-lock
npm --prefix web run build     # tsc -b + vite build — type-check IS part of the gate
npm --prefix web run test:cov  # vitest run --coverage; enforces the threshold floor
```
- `test:cov` fails the run if coverage drops below the §5 floor (the ratchet).
- `npm run build` is included in `full` because `tsc -b` is the type-safety gate; a green vitest run with a type error must still fail CI.
- **Tier 3 (Playwright)** is invoked separately by the cross-domain layer (e.g. `npm --prefix web run e2e` once it exists) against a live backend — it is NOT part of `web`'s `test`/`test:cov` and must not be required for the unit/component gate to pass.

---

## 8. Migration impact

### Churn to bring the 10 existing files onto the standard
The existing files are already close to the standard — this is **low churn**, mostly additive:

| Change | Files affected | Effort |
|---|---|---|
| Add `web/src/test/{server,handlers,render}.tsx` + MSW dep | new files | ~1 hr |
| Expand `setup.ts` (MSW lifecycle + resets) | `setup.ts` | small; existing tests already self-reset, so no breakage |
| Relocate root-level component tests next to source | `StatusDot.test.tsx`, `Toaster.test.tsx`, `SearchPageGating.test.tsx` → `src/components/` & `src/pages/` | trivial `git mv` + import path fixups |
| Commit `@vitest/coverage-v8`, add `coverage`/threshold config + scripts | `vitest.config.ts`, `package.json` | small |
| Optionally migrate `SearchPageGating`'s hand-rolled `fetch` stub to MSW | `SearchPageGating.test.tsx` | optional; leave as-is (it works) or convert as the reference Tier-2 example |
| Leave Tier-1 tests untouched | `capabilities`, `statusDerive`, `observedFeed`, `toast`, `health`, `healthPoll`, `api.getStatus` | none — they already match |

**Net:** no test rewrites required; the 10 files keep passing. The standard is mostly *new scaffolding* (test-utils + config) plus 3 file moves. Estimate **~half a day** to land the standard with zero behavior change to existing tests.

### First 5 high-value feature components to add tests for
Chosen for **highest logic × highest risk**, matching audit P2 (start with traffic/logic, not the easy renders):

1. **`pages/Chat.tsx` (955 LOC)** — compose box + the three modes (Entry / Research / Full Brain), send flow, SSE event handling, mode gating against capabilities. Tier 2: render, type, send; assert correct endpoint + mode; assert read-only modes mutate nothing. Stub `streamChat` SSE.
2. **`pages/SharesPage.tsx` + `components/EncryptedChat.tsx` (496 + 218 LOC)** — Web Crypto round-trip (channel-key wrap/unwrap via `crypto.ts`), bind, owner↔guest message flow. High risk (security/medical) and entirely untested. Tier 2 with real `crypto.subtle`, MSW for the relay endpoints.
3. **`pages/NotePage.tsx` (492 LOC)** — view / edit / save / version history / diff, concurrency on save. Core write path. Tier 2: edit→save asserts `PUT`, surfaces `ApiError` as a toast.
4. **`components/RebuildPanel.tsx` (480 LOC)** — two-stage gather→curate→draft SSE orchestration, source selection, accept/reject. Complex client state machine over `streamSSE`. Tier 2 with a scripted SSE stub.
5. **`pages/CalendarPage.tsx` (570 LOC)** — day/week/month projection, quick-add, reminders, timezone handling. TZ + date math is a determinism hazard, so this also validates the fake-timer/fixed-clock rules from §6.

Each lands meaningful coverage on a currently-0% high-LOC surface; after these five, raise the coverage floor (§5) from 7% toward ~25% and continue ratcheting.

---

## Appendix — quick reference

- **Tiers:** 1 unit (pure, in-process) · 2 component-integration (RTL + jsdom + MSW) · 3 system/E2E (Playwright, separate, real stack).
- **Layout:** colocated `*.test.tsx` next to source; shared utils in `src/test/`.
- **Network:** MSW for Tier 2 (`onUnhandledRequest: "error"`); hand-rolled `vi.stubGlobal("fetch")` for Tier-1 api-client + SSE tests.
- **Helper:** `renderWithProviders(ui, { route })` → `{ user, ...RTL }`.
- **Run:** unit-only `npm --prefix web run test`; full `npm --prefix web run build && npm --prefix web run test:cov`.
- **Determinism:** fake timers, seeded/real WebCrypto, mocked geo/Notification, no real network, reset singletons.
