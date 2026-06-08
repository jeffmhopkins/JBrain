// Component-integration (Tier 2) tests for <AiAnalysisPanel> — a note's read-only AI
// analysis sidecar (gist / salient facts / entities / dates / domain). On mount it GETs
// the cached analysis; the ↻ button force-recomputes it (POST) and follows a rename if
// the title check produced a new slug. The control is gated on the llm capability.
//
// Network: every endpoint is mocked via MSW (onUnhandledRequest:"error").
// Health: seed the health store so useCapability("llm") reports ready (mirrors NotePage).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router-dom";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { http, HttpResponse } from "msw";
import { __reset, ingestVerify } from "../health";

import AiAnalysisPanel from "./AiAnalysisPanel";

const READY_CAPS = {
  llm: { state: "ready", providers: { anthropic: true, xai: false } },
  embeddings: { state: "ready" }, transcription: { state: "ready" },
  push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
};
const ABSENT_LLM = { ...READY_CAPS, llm: { state: "absent", providers: { anthropic: false, xai: false } } };

beforeEach(() => {
  __reset();
  ingestVerify({ capabilities: READY_CAPS });
});
afterEach(() => cleanup());

// Render inside a /note/:slug route so the rename navigate() resolves to a real route.
function renderPanel(slug = "groceries") {
  return renderWithProviders(
    <Routes>
      <Route path="/note/:slug" element={<AiAnalysisPanel slug={slug} />} />
    </Routes>,
    { route: `/note/${slug}` },
  );
}

describe("AiAnalysisPanel", () => {
  it("renders the loaded gist, facts and entity chips", async () => {
    server.use(http.get("*/api/notes/:slug/analysis", () => HttpResponse.json({
      gist: "A short summary.",
      facts: ["Buy milk", "Buy eggs"],
      entities: [{ type: "person", name: "Sarah" }],
      domain: "Cooking",
    })));
    renderPanel();

    expect(await screen.findByText("A short summary.")).toBeInTheDocument();
    expect(screen.getByText("Buy milk")).toBeInTheDocument();
    // Entity chip links to the entities view.
    const chip = screen.getByRole("link", { name: /Sarah/ });
    expect(chip).toHaveAttribute("href", expect.stringContaining("/entities?type=person"));
    // The domain badge renders.
    expect(screen.getByText("Cooking")).toBeInTheDocument();
  });

  it("shows the not-analyzed-yet hint when there is no cached analysis", async () => {
    server.use(http.get("*/api/notes/:slug/analysis", () => HttpResponse.json({})));
    renderPanel();

    expect(await screen.findByText(/Not analyzed yet/)).toBeInTheDocument();
    // The analyze control is present + enabled (llm ready).
    expect(screen.getByRole("button", { name: /Analyze \+ title this note/ })).toBeEnabled();
  });

  it("re-analyze POSTs and renders the freshly-computed analysis", async () => {
    let posted = false;
    server.use(
      http.get("*/api/notes/:slug/analysis", () => HttpResponse.json({})),
      http.post("*/api/notes/:slug/analysis", () => {
        posted = true;
        return HttpResponse.json({ gist: "Freshly generated gist.", facts: [], entities: [] });
      }),
    );
    const { user } = renderPanel();

    await screen.findByText(/Not analyzed yet/);
    await user.click(screen.getByRole("button", { name: /Analyze \+ title this note/ }));

    await waitFor(() => expect(posted).toBe(true));
    expect(await screen.findByText("Freshly generated gist.")).toBeInTheDocument();
  });

  it("follows a rename: navigates to the new slug when the title check renamed the note", async () => {
    server.use(
      http.get("*/api/notes/:slug/analysis", () => HttpResponse.json({})),
      http.post("*/api/notes/:slug/analysis", () =>
        HttpResponse.json({ slug: "groceries-renamed", gist: "ignored on this slug" })),
    );
    const { user } = renderWithProviders(
      <Routes>
        <Route path="/note/groceries" element={<AiAnalysisPanel slug="groceries" />} />
        <Route path="/note/groceries-renamed" element={<div>RENAMED PAGE</div>} />
      </Routes>,
      { route: "/note/groceries" },
    );

    await screen.findByText(/Not analyzed yet/);
    await user.click(screen.getByRole("button", { name: /Analyze \+ title this note/ }));

    expect(await screen.findByText("RENAMED PAGE")).toBeInTheDocument();
  });

  it("disables the analyze control when the llm capability is absent", async () => {
    __reset();
    ingestVerify({ capabilities: ABSENT_LLM });
    server.use(http.get("*/api/notes/:slug/analysis", () => HttpResponse.json({})));
    renderPanel();

    await screen.findByText(/Not analyzed yet/);
    // Gated off: the button is disabled and its title carries the cap reason.
    const btn = screen.getByRole("button");
    expect(btn).toBeDisabled();
  });

  it("treats an analysis load error as not-analyzed (no crash)", async () => {
    server.use(http.get("*/api/notes/:slug/analysis", () =>
      HttpResponse.json({ detail: "nope" }, { status: 500 })));
    renderPanel();

    expect(await screen.findByText(/Not analyzed yet/)).toBeInTheDocument();
  });
});
