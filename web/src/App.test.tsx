// Component-integration (Tier 2) tests for App.tsx — the application root: the
// AuthCtx/useAuth provider, the access-key gate, the boot-time verify() that decides
// authed-vs-gate, the server-version-mismatch banner (computed in App, rendered by
// Shell), and the disconnect/clear-key path.
//
// App is the ROOT, so unlike sibling tests we do NOT vi.mock("./App") — we render the
// REAL component and keep its own logic (boot effect, connect/disconnect, version
// compare, gate routing) live. App provides its own <Routes> but no Router, so we
// mount it via renderWithProviders (which supplies a MemoryRouter); `route` sets the
// initial path. Heavy authed route children (Chat) are stubbed so a successful login
// renders a light shell instead of pulling the whole chat stack. Shell stays REAL so
// the brand + the version banner (driven by App's computed state) are exercised.
//
// Network is MSW only (setup.ts runs onUnhandledRequest:"error"), so every endpoint
// App + Shell hit on boot is stubbed: /api/auth/info, /api/auth/verify (the boot/login
// requests), the /api/system/status poll (useHealthPoll), and /api/reviews/count (the
// Shell's ReviewBell). The access key is seeded per-test via setAccessKey() — App reads
// getAccessKey() at boot to decide whether to verify or show the gate.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, waitFor, cleanup } from "./test/render";
import { server } from "./test/server";
import { http, HttpResponse } from "msw";
import { __reset } from "./health";
import { READY_CAPS } from "./test/handlers";
import { setAccessKey, clearAccessKey } from "./api";

// A toggle (set per-test) lets the single Chat stub double as a disconnect probe: when on,
// the home page also renders a button wired to App's disconnect() — reached through the REAL
// AuthCtx via useAuth — so the logout path runs without the whole shell menu. vi.hoisted so
// the value exists when the (hoisted) vi.mock factory runs.
const probe = vi.hoisted(() => ({ disconnect: false }));

// Chat is the home route (/ redirects to /chat) and pulls in a heavy stack — stub it so
// a successful login lands on a light, identifiable page while App's own gate/shell run.
// The factory pulls useAuth lazily from the (real) App module via the test's own import map.
vi.mock("./pages/Chat", () => ({
  default: function ChatStub() {
    // Imported below as a normal ESM binding; safe to reference at render time.
    const { disconnect } = useAuth();
    return (
      <div data-testid="chat-page">
        Chat page
        {probe.disconnect && <button onClick={disconnect}>do-disconnect</button>}
      </div>
    );
  },
}));

// The ReviewBell inside the real Shell polls this on mount; baseline so the authed
// render never trips the unhandled-request error.
function reviewHandler() {
  return http.get("*/api/reviews/count", () => HttpResponse.json({ pending: 0 }));
}

// PWA_VERSION under the test runner is npm_package_version (see vitest.config.ts define).
// We don't hard-code it; instead the version-match test echoes the verify version back
// from the same source so the comparison is deterministic regardless of the package.
import { PWA_VERSION } from "./App";

import App, { useAuth } from "./App";

beforeEach(() => {
  __reset();
  clearAccessKey();
  probe.disconnect = false;
  server.use(reviewHandler());
});
afterEach(() => cleanup());

describe("App — access-key gate (no stored key)", () => {
  it("shows the KeyEntry gate and NO authed shell when there is no stored key", async () => {
    let verifyHits = 0;
    server.use(
      http.get("*/api/auth/info", () => HttpResponse.json({ brain_name: "Gateless Brain" })),
      http.get("*/api/auth/verify", () => { verifyHits += 1; return new HttpResponse(null, { status: 401 }); }),
    );

    renderWithProviders(<App />, { route: "/chat" });

    // The gate renders: brain name as the heading + the connect form.
    expect(await screen.findByRole("heading", { name: "Gateless Brain" })).toBeInTheDocument();
    expect(screen.getByText("Connect this device to your brain")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument();
    // No authed content: no shell brand, no stubbed home page.
    expect(screen.queryByTestId("chat-page")).not.toBeInTheDocument();
    expect(document.querySelector(".brand")).toBeNull();
    // With no stored key App must NOT call verify() on boot — it goes straight to the gate.
    expect(verifyHits).toBe(0);
  });
});

describe("App — boot with a valid stored key", () => {
  it("runs verify() on boot and renders the app shell + home page", async () => {
    let verifyHits = 0;
    server.use(
      http.get("*/api/auth/info", () => HttpResponse.json({ brain_name: "Booted Brain" })),
      http.get("*/api/auth/verify", () => {
        verifyHits += 1;
        return HttpResponse.json({ version: "test", has_llm: true, owner_set: true, capabilities: READY_CAPS });
      }),
    );
    setAccessKey("good-key");

    renderWithProviders(<App />, { route: "/chat" });

    // Boot verify() ran exactly once and the authed shell + stubbed home render.
    expect(await screen.findByTestId("chat-page")).toBeInTheDocument();
    expect(screen.getByText("Booted Brain")).toBeInTheDocument(); // Shell brand
    expect(document.querySelector(".brand")).toBeInTheDocument();
    // The gate is gone.
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
    await waitFor(() => expect(verifyHits).toBe(1));
  });

  it("does NOT show the version banner when server version matches the PWA", async () => {
    server.use(
      http.get("*/api/auth/info", () => HttpResponse.json({ brain_name: "Match Brain" })),
      // Echo the PWA's own version so versionMismatch is false.
      http.get("*/api/auth/verify", () =>
        HttpResponse.json({ version: PWA_VERSION, has_llm: true, owner_set: true, capabilities: READY_CAPS })),
    );
    setAccessKey("good-key");

    renderWithProviders(<App />, { route: "/chat" });

    await screen.findByTestId("chat-page");
    expect(document.querySelector(".version-banner")).toBeNull();
  });
});

describe("App — server version mismatch", () => {
  it("renders the version-mismatch banner when the server version differs from the PWA", async () => {
    server.use(
      http.get("*/api/auth/info", () => HttpResponse.json({ brain_name: "Mismatch Brain" })),
      http.get("*/api/auth/verify", () =>
        HttpResponse.json({ version: "9.9.9-server", has_llm: true, owner_set: true, capabilities: READY_CAPS })),
    );
    setAccessKey("good-key");

    renderWithProviders(<App />, { route: "/chat" });

    await screen.findByTestId("chat-page");
    const banner = await waitFor(() => {
      const b = document.querySelector(".version-banner");
      expect(b).toBeInTheDocument();
      return b!;
    });
    expect(banner).toHaveTextContent("server v9.9.9-server");
    expect(banner).toHaveTextContent(/versions differ; update from System/);
  });
});

describe("App — connect via the gate (failed then successful verify)", () => {
  it("a rejected key (401 verify) surfaces the error and keeps the gate up", async () => {
    server.use(
      http.get("*/api/auth/info", () => HttpResponse.json({ brain_name: "Strict Brain" })),
      http.get("*/api/auth/verify", () => new HttpResponse(null, { status: 401 })),
    );
    // No stored key → boot lands on the gate without calling verify.
    const { user } = renderWithProviders(<App />, { route: "/chat" });

    await screen.findByRole("button", { name: "Connect" });
    await user.type(screen.getByPlaceholderText(/Paste the access key/i), "bad-key");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    // connect() throws on the 401; KeyEntry shows the rejected-key message and stays put.
    expect(await screen.findByText(/That access key was rejected/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument();
    expect(screen.queryByTestId("chat-page")).not.toBeInTheDocument();
  });

  it("a valid key submitted in the gate calls info + verify and enters the authed shell", async () => {
    let infoHits = 0;
    let verifyHits = 0;
    server.use(
      http.get("*/api/auth/info", () => { infoHits += 1; return HttpResponse.json({ brain_name: "Live Brain" }); }),
      http.get("*/api/auth/verify", () => {
        verifyHits += 1;
        return HttpResponse.json({ version: "test", has_llm: true, owner_set: true, capabilities: READY_CAPS });
      }),
    );
    const { user } = renderWithProviders(<App />, { route: "/chat" });

    await screen.findByRole("button", { name: "Connect" });
    const verifyBefore = verifyHits;
    await user.type(screen.getByPlaceholderText(/Paste the access key/i), "good-key");
    await user.click(screen.getByRole("button", { name: "Connect" }));

    // connect() resolves info then verify, then flips authed → the stubbed home renders.
    expect(await screen.findByTestId("chat-page")).toBeInTheDocument();
    expect(screen.getByText("Live Brain")).toBeInTheDocument();
    expect(verifyHits).toBe(verifyBefore + 1); // verify() fired on connect
    expect(infoHits).toBeGreaterThanOrEqual(1); // info() resolves the server first
  });
});

describe("App — disconnect clears the key and returns to the gate", () => {
  it("disconnect() drops the stored key and re-shows KeyEntry", async () => {
    server.use(
      http.get("*/api/auth/info", () => HttpResponse.json({ brain_name: "Logout Brain" })),
      http.get("*/api/auth/verify", () =>
        HttpResponse.json({ version: "test", has_llm: true, owner_set: true, capabilities: READY_CAPS })),
    );
    setAccessKey("good-key");

    // The Chat stub (home route) renders App's disconnect() — reached through the REAL
    // AuthCtx via useAuth — as a button when this probe flag is on.
    probe.disconnect = true;

    const { user } = renderWithProviders(<App />, { route: "/chat" });

    // Boot authed: probe button visible inside the real shell.
    const btn = await screen.findByRole("button", { name: "do-disconnect" });
    expect(localStorage.getItem("jbrain_access_key")).toBe("good-key");

    await user.click(btn);

    // disconnect() cleared the key and flipped authed=false → KeyEntry is back.
    expect(await screen.findByRole("button", { name: "Connect" })).toBeInTheDocument();
    expect(localStorage.getItem("jbrain_access_key")).toBeNull();
    expect(screen.queryByRole("button", { name: "do-disconnect" })).not.toBeInTheDocument();
  });
});
