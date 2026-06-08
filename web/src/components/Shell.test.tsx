// Component-integration (Tier 2) tests for Shell.tsx — the app shell / navigation
// chrome (top bar brand + tool title, the StatusDot health indicator, the ReviewBell
// dropdown, the TTS toggle, the Advanced "bolt" launcher, and the version/offline
// banners). MSW is the only network mock (setup.ts runs onUnhandledRequest: "error"),
// so every endpoint the shell touches on mount is stubbed: ReviewBell polls
// /api/reviews/count and lazily GETs /api/reviews on open. StatusDot + the banners
// read the health store directly (no network — no poller is mounted here), seeded via
// ingestVerify()/setOnline()/applyPoll(). useAuth() is stubbed from ../App with a full
// AuthState shape so version/brand state is controllable per test without the real App tree.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router-dom";
import { renderWithProviders, screen, waitFor, within, cleanup } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import { __reset, ingestVerify, setOnline, applyPoll } from "../health";
import { READY_CAPS } from "../test/handlers";

// Shell reads a lot of auth/version state via useAuth() (brainName, versionMismatch,
// pwaVersion, serverVersion, vapidPublicKey). ReviewBell also reads vapidPublicKey.
// A mutable holder lets each test tweak the snapshot before render. Shape mirrors
// AuthState in ../App.
let authValue: Record<string, unknown>;
function resetAuth() {
  authValue = {
    authenticated: true,
    brainName: "Test Brain",
    server: "",
    pwaVersion: "1.0.0",
    serverVersion: "1.0.0",
    versionMismatch: false,
    hasLlm: true,
    appTz: "UTC",
    vapidPublicKey: "",
    connect: vi.fn(),
    disconnect: vi.fn(),
  };
}
vi.mock("../App", () => ({
  useAuth: () => authValue,
  PWA_VERSION: "1.0.0",
}));

import Shell from "./Shell";
import AdvancedHome from "../pages/AdvancedHome";

// ReviewBell fires GET /api/reviews/count on mount and GET /api/reviews on open.
// Register baseline handlers so the default render never hits the unhandled-request
// error; per-test server.use(...) overrides them where the bell matters.
function reviewHandlers(count = 0, items: unknown[] = []) {
  return [
    http.get("*/api/reviews/count", () => HttpResponse.json({ pending: count })),
    http.get("*/api/reviews", () => HttpResponse.json(items)),
  ];
}

// Mount the shell inside a real route table so the bolt's nav("/advanced"),
// nav("/chat") and a full-screen tool route all resolve to a distinguishable child.
// The shell's own chrome (brand vs tool-title, active bolt) is what we assert.
function renderShell(route: string) {
  return renderWithProviders(
    <Shell>
      <Routes>
        <Route path="/chat" element={<div data-testid="page">Compose page</div>} />
        <Route path="/advanced" element={<AdvancedHome />} />
        <Route path="/wiki" element={<div data-testid="page">Wiki page</div>} />
        <Route path="/review" element={<div data-testid="page">Review page</div>} />
        <Route path="/notifications" element={<div data-testid="page">History page</div>} />
        <Route path="*" element={<div data-testid="page">Other</div>} />
      </Routes>
    </Shell>,
    { route },
  );
}

// The Advanced bolt has no accessible name beyond its title attribute ("Advanced" /
// "Back to compose"); grab it by class. It's the last .bolt button in the top bar
// (after the review bell and the TTS toggle).
function bolt(): HTMLButtonElement {
  const btns = Array.from(document.querySelectorAll("button.bolt"));
  const b = btns[btns.length - 1] as HTMLButtonElement;
  if (!b) throw new Error("no bolt button found");
  return b;
}

beforeEach(() => {
  __reset();
  resetAuth();
  ingestVerify({ capabilities: READY_CAPS });   // seed health: all-ready, reachable
  server.use(...reviewHandlers());
});
afterEach(() => cleanup());

describe("Shell — header & nav chrome", () => {
  it("renders the brand on a primary route and the core chrome controls", async () => {
    renderShell("/chat");

    // Brand shows the brain name — not a tool title.
    expect(screen.getByText("Test Brain")).toBeInTheDocument();
    expect(document.querySelector(".brand")).toBeInTheDocument();
    expect(document.querySelector(".tool-title")).toBeNull();

    // The child page renders inside the shell body.
    expect(screen.getByTestId("page")).toHaveTextContent("Compose page");

    // Status indicator, review bell and TTS toggle are all present.
    expect(screen.getByRole("button", { name: /Server status/ })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /to review/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Read replies aloud/ })).toBeInTheDocument();

    // On /chat the bolt is the launcher (not active) and titled "Advanced".
    expect(bolt().title).toBe("Advanced");
    expect(bolt().classList.contains("active")).toBe(false);
  });

  it("shows a back chevron + tool title (not the brand) when a tool is open full-screen", () => {
    renderShell("/wiki");

    expect(screen.getByText("Wiki")).toBeInTheDocument();
    expect(document.querySelector(".tool-title")).toHaveTextContent("Wiki");
    expect(document.querySelector(".brand")).toBeNull();
    expect(screen.getByRole("button", { name: "Back" })).toBeInTheDocument();
    // The bolt is "active" on any advanced surface and offers a way back to compose.
    expect(bolt().title).toBe("Back to compose");
    expect(bolt().classList.contains("active")).toBe(true);
  });
});

describe("Shell — Advanced launcher navigation", () => {
  it("the bolt navigates to /advanced and the grouped launcher grid renders", async () => {
    const { user } = renderShell("/chat");

    expect(screen.getByTestId("page")).toHaveTextContent("Compose page");
    await user.click(bolt());

    // /advanced is the launcher home: grouped sections + cards render, bolt is active.
    // ("System" appears both as a section header and a card label, so scope the
    // section assertions to the .adv-section headers.)
    await screen.findByText("Knowledge");
    const sections = Array.from(document.querySelectorAll(".adv-section")).map((e) => e.textContent);
    expect(sections).toEqual(["Knowledge", "Authoring", "System"]);
    expect(screen.getByRole("button", { name: /Wiki/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Triggers/ })).toBeInTheDocument();
    expect(bolt().classList.contains("active")).toBe(true);
    // The launcher grid keeps the brand (it's advHome, not a full-screen tool).
    expect(document.querySelector(".brand")).toBeInTheDocument();
  });

  it("picking a card opens that tool full-screen, and the back chevron returns", async () => {
    const { user } = renderShell("/advanced");

    await user.click(await screen.findByRole("button", { name: /Wiki/ }));

    // Now on /wiki: the tool title replaces the brand and a Back chevron appears.
    expect(await screen.findByText("Wiki")).toBeInTheDocument();
    expect(document.querySelector(".tool-title")).toHaveTextContent("Wiki");

    await user.click(screen.getByRole("button", { name: "Back" }));
    // Browser/router back returns to the launcher grid.
    expect(await screen.findByText("Knowledge")).toBeInTheDocument();
  });

  it("on an advanced surface the bolt returns to compose", async () => {
    const { user } = renderShell("/advanced");
    await screen.findByText("Knowledge");

    expect(bolt().title).toBe("Back to compose");
    await user.click(bolt());

    expect(await screen.findByText("Compose page")).toBeInTheDocument();
    expect(bolt().title).toBe("Advanced");
  });
});

describe("Shell — review bell dropdown", () => {
  it("shows a pending count badge from the count poll", async () => {
    server.use(...reviewHandlers(3));
    renderShell("/chat");

    const bell = await screen.findByRole("button", { name: /to review/ });
    await waitFor(() => expect(within(bell).getByText("3")).toBeInTheDocument());
    expect(bell.title).toBe("3 to review");
  });

  it("opens the dropdown, lists items, and dismiss POSTs + removes the item", async () => {
    const items = [
      { id: 7, title: "Review this note", message: "Looks stale", link_slug: "groceries", created_at: "2024-01-01T00:00:00Z" },
    ];
    let dismissed = 0;
    server.use(
      ...reviewHandlers(1, items),
      http.post("*/api/reviews/7/dismiss", () => { dismissed += 1; return HttpResponse.json({ ok: true }); }),
    );
    const { user } = renderShell("/chat");

    const bell = await screen.findByRole("button", { name: /to review/ });
    await user.click(bell);

    // The dropdown opens with the history doorway and the listed item.
    expect(await screen.findByText("Notification History")).toBeInTheDocument();
    expect(screen.getByText("Review this note")).toBeInTheDocument();
    expect(screen.getByText("Looks stale")).toBeInTheDocument();
    // An Open link points at the item's note.
    expect(screen.getByRole("link", { name: "Open" })).toHaveAttribute("href", "/note/groceries");

    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(dismissed).toBe(1));
    // After dismiss the item is gone and the empty state shows.
    await waitFor(() => expect(screen.queryByText("Review this note")).not.toBeInTheDocument());
    expect(screen.getByText("No new notifications.")).toBeInTheDocument();
  });

  it("Notification History link navigates to /notifications and closes the menu", async () => {
    const { user } = renderShell("/chat");
    const bell = await screen.findByRole("button", { name: /to review/ });
    await user.click(bell);

    await user.click(await screen.findByText("Notification History"));
    expect(await screen.findByText("History page")).toBeInTheDocument();
  });
});

describe("Shell — TTS toggle", () => {
  it("toggles the read-aloud state and reflects it in the button", async () => {
    const { user } = renderShell("/chat");

    const tts = screen.getByRole("button", { name: /Read replies aloud/ });
    expect(tts.getAttribute("aria-pressed")).toBe("false");

    await user.click(tts);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Read replies aloud: on/ }).getAttribute("aria-pressed"),
      ).toBe("true"));
  });
});

describe("Shell — review route done affordance", () => {
  it("on /review shows a Done button and no Advanced bolt; Done returns to compose", async () => {
    const { user } = renderShell("/review");

    expect(screen.getByText("Review page")).toBeInTheDocument();
    // /review swaps the bolt launcher for a "Done" action — no Advanced/compose bolt.
    const done = screen.getByRole("button", { name: "Done" });
    const advBolt = Array.from(document.querySelectorAll("button.bolt"))
      .some((b) => /Advanced|compose/.test((b as HTMLButtonElement).title));
    expect(advBolt).toBe(false);

    await user.click(done);
    expect(await screen.findByText("Compose page")).toBeInTheDocument();
  });
});

describe("Shell — health indicator & banners", () => {
  it("StatusDot reflects an all-ready capability set (no problem level)", () => {
    renderShell("/chat");
    const status = screen.getByRole("button", { name: /Server status/ });
    // All ready ⇒ the summary in the title is the all-clear, and the dot carries `ok`.
    expect(status.getAttribute("title")).toMatch(/All systems ready/);
    expect(status.querySelector(".status-dot.ok")).toBeInTheDocument();
  });

  it("StatusDot turns down when the server is unreachable", () => {
    applyPoll({ ok: false, reason: "unreachable" });
    renderShell("/chat");
    const status = screen.getByRole("button", { name: /Server status/ });
    expect(status.querySelector(".status-dot.down")).toBeInTheDocument();
    expect(status.getAttribute("title")).toMatch(/Can.t reach Test Brain/);
  });

  it("renders the version-mismatch banner when app and server versions differ", () => {
    authValue.versionMismatch = true;
    authValue.pwaVersion = "1.0.0";
    authValue.serverVersion = "2.0.0";
    renderShell("/chat");

    const banner = document.querySelector(".version-banner");
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent("App v1.0.0 vs server v2.0.0");
    expect(banner).toHaveTextContent(/update from System/);
  });

  it("renders the browser-offline banner when the device has no network", () => {
    setOnline(false);
    renderShell("/chat");
    expect(document.querySelector(".offline-banner")).toHaveTextContent(/Offline — reading cached notes only/);
  });

  it("renders the server-unreachable banner naming the brain when only the server is down", () => {
    applyPoll({ ok: false, reason: "unreachable" });
    renderShell("/chat");
    const banner = document.querySelector(".offline-banner");
    expect(banner).toHaveTextContent(/Test Brain server unreachable/);
  });
});
