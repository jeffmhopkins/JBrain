import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll } from "vitest";
import { server } from "./server";

// MSW: the only sanctioned network mock for component-integration (Tier 2) tests.
// Unhandled requests are an ERROR, not a silent miss — a page hitting an un-mocked
// endpoint fails loudly. Tier-1 tests that vi.stubGlobal("fetch", ...) replace fetch
// for their duration (MSW bypassed there) and are restored by unstubGlobals.
//
// NB: do NOT import app modules (health/toast/api) here — eagerly evaluating them at
// setup time binds their real deps and defeats per-file vi.mock("./api"). Tests reset
// their own singletons (the existing suite already calls __reset()/__resetToasts()).
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers(); // drop per-test http.* overrides
  localStorage.clear();
  sessionStorage.clear();
});
afterAll(() => server.close());
