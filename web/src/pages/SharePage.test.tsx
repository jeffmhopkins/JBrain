// Component-integration (Tier 2) tests for SharePage — the PUBLIC recipient view of a
// shared note/link opened via /share/:token by someone who is NOT the owner.
//
// SharePage talks ONLY to the cookie-auth `publicApi` share endpoints (getShare /
// claimShare / proposeShareEdit, see api.ts). MSW is the only network mock and
// setup.ts runs onUnhandledRequest:"error", so every endpoint the page hits per flow
// is stubbed. The page reads its token from useParams(), so each test renders inside a
// real <Routes><Route path="/share/:token"/></Routes> with route:"/share/TOKEN".
//
// The AI-backed kinds (guided/research/chat/labs) are delegated to self-contained child
// components; here we assert SharePage's OWN dispatch/gating branches and the note
// view + claim/bind + propose-edit flows it renders directly. No production file is
// modified.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";

import SharePage from "./SharePage";

// A complete share-view note payload as the public GET returns it. Overridable per test.
function shareView(over: Record<string, unknown> = {}) {
  return {
    scope: "view",
    can_edit: false,
    brain_name: "Test Brain",
    app_tz: "UTC",
    bound_name: null,
    note: {
      title: "notes/Trip Plan",
      content_md: "Pack **socks** and a map.",
      kind: "note",
      updated_at: "2024-01-01T00:00:00Z",
      attachments: [],
    },
    ...over,
  };
}

// Render SharePage at /share/:token so useParams() resolves the token.
function renderShare(token = "TOKEN") {
  return renderWithProviders(
    <Routes><Route path="/share/:token" element={<SharePage />} /></Routes>,
    { route: `/share/${token}` },
  );
}

const TOKEN_PATH = "*/api/share/:token";

beforeEach(() => { localStorage.clear(); });
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) Valid token → the shared note content renders read-only.
// ---------------------------------------------------------------------------- //
describe("SharePage — read-only note view", () => {
  it("renders the shared note's title, markdown body and read-only badge", async () => {
    server.use(http.get(TOKEN_PATH, () => HttpResponse.json(shareView())));
    renderShare();

    // Leaf title (the "notes/" prefix is stripped).
    expect(await screen.findByRole("heading", { name: "Trip Plan", level: 1 })).toBeInTheDocument();
    // Markdown body comes through react-markdown (bold span).
    expect(await screen.findByText("socks")).toBeInTheDocument();
    // Read-only badge + owner attribution footer.
    expect(screen.getByText("Shared · read-only")).toBeInTheDocument();
    expect(screen.getByText(/Shared from Test Brain/)).toBeInTheDocument();
    // No "Suggest an edit" control on a non-editable share.
    expect(screen.queryByRole("button", { name: /Suggest an edit/ })).not.toBeInTheDocument();
  });

  it("requests the share with the token from the route", async () => {
    let hitUrl = "";
    server.use(http.get(TOKEN_PATH, ({ request }) => {
      hitUrl = request.url;
      return HttpResponse.json(shareView());
    }));
    renderShare("abc123");

    await screen.findByRole("heading", { name: "Trip Plan", level: 1 });
    expect(hitUrl).toContain("/api/share/abc123");
  });

  it("renders an image attachment via the token-scoped attachment url", async () => {
    server.use(http.get(TOKEN_PATH, () => HttpResponse.json(shareView({
      note: {
        title: "notes/Photos", content_md: "See below.", kind: "note",
        updated_at: "2024-01-01T00:00:00Z",
        attachments: [{ id: 42, filename: "beach.png", mime: "image/png", byte_size: 10 }],
      },
    }))));
    renderShare();

    const img = await screen.findByAltText("beach.png") as HTMLImageElement;
    // The src is the public token-scoped attachment endpoint (not fetched by jsdom).
    expect(img.getAttribute("src")).toContain("/api/share/TOKEN/attachments/42");
  });
});

// ---------------------------------------------------------------------------- //
// (b) Invalid / expired / locked token → the error gate (no note content).
// ---------------------------------------------------------------------------- //
describe("SharePage — error gate", () => {
  it("shows the not-available gate for a 404 (revoked/expired)", async () => {
    server.use(http.get(TOKEN_PATH, () =>
      HttpResponse.json({ detail: "gone" }, { status: 404 })));
    renderShare();

    expect(await screen.findByText("This link isn't available.")).toBeInTheDocument();
    expect(screen.getByText(/revoked, expired, or never existed/)).toBeInTheDocument();
  });

  it("shows the locked gate for a 403 (bound to another browser)", async () => {
    server.use(http.get(TOKEN_PATH, () =>
      HttpResponse.json({ detail: "locked" }, { status: 403 })));
    renderShare();

    expect(await screen.findByText("This link is locked.")).toBeInTheDocument();
    expect(screen.getByText(/opened in another browser first/)).toBeInTheDocument();
  });

  it("shows the rate-limit gate for a 429", async () => {
    server.use(http.get(TOKEN_PATH, () =>
      HttpResponse.json({ detail: "slow down" }, { status: 429 })));
    renderShare();

    expect(await screen.findByText("Too many requests.")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (c) AI-backed kind that's temporarily unavailable (llm_ready:false) — a
//     SharePage-owned pre-flight gate, distinct from the chat child.
// ---------------------------------------------------------------------------- //
describe("SharePage — AI kind gating", () => {
  it("shows 'Temporarily unavailable' for a guided share with llm_ready:false", async () => {
    server.use(http.get(TOKEN_PATH, () => HttpResponse.json(shareView({
      kind: "guided", llm_ready: false, note: undefined,
    }))));
    renderShare();

    expect(await screen.findByText("Temporarily unavailable")).toBeInTheDocument();
    expect(screen.getByText(/isn’t available right now/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (d) Bind-to-first-device: the consent / claim landing for an unaccepted link.
// ---------------------------------------------------------------------------- //
describe("SharePage — consent / claim (bind to first device)", () => {
  it("read-only bind: shows the consent landing and claims on Accept, then renders the note", async () => {
    let claimUrl = "";
    server.use(
      // First load returns requires_claim; the claim POST returns the unlocked view.
      http.get(TOKEN_PATH, () => HttpResponse.json(shareView({ requires_claim: true, note: undefined }))),
      http.post("*/api/share/:token/claim", ({ request }) => {
        claimUrl = request.url;
        return HttpResponse.json(shareView()); // claimed -> full note view
      }),
    );
    const { user } = renderShare();

    // Consent landing: read-only copy, no name input.
    expect(await screen.findByText("Accept to view")).toBeInTheDocument();
    expect(screen.getByText(/only this browser will be able to open this link/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Your name *")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Accept & view/ }));

    await waitFor(() => expect(claimUrl).toContain("/api/share/TOKEN/claim"));
    // The note now renders.
    expect(await screen.findByRole("heading", { name: "Trip Plan", level: 1 })).toBeInTheDocument();
  });

  it("editable bind: requires a name, sends it on claim, and persists it locally", async () => {
    let claimBody: any = null;
    server.use(
      http.get(TOKEN_PATH, () =>
        HttpResponse.json(shareView({ requires_claim: true, can_edit: true, scope: "edit", note: undefined }))),
      http.post("*/api/share/:token/claim", async ({ request }) => {
        claimBody = await request.json();
        return HttpResponse.json(shareView({ can_edit: true, scope: "edit", bound_name: "Pat" }));
      }),
    );
    const { user } = renderShare();

    expect(await screen.findByText("Accept to start editing")).toBeInTheDocument();
    const nameInput = screen.getByPlaceholderText("Your name *");
    await user.type(nameInput, "Pat");
    await user.click(screen.getByRole("button", { name: /Accept & continue/ }));

    await waitFor(() => expect(claimBody).toEqual({ name: "Pat" }));
    // The name is remembered for next time.
    expect(localStorage.getItem("jbrain_share_name")).toBe("Pat");
    // Editable note view renders with the edit affordance.
    expect(await screen.findByRole("button", { name: /Suggest an edit/ })).toBeInTheDocument();
  });

  it("editable claim with an empty name alerts and does not POST", async () => {
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    let claimed = false;
    server.use(
      http.get(TOKEN_PATH, () =>
        HttpResponse.json(shareView({ requires_claim: true, can_edit: true, scope: "edit", note: undefined }))),
      http.post("*/api/share/:token/claim", () => { claimed = true; return HttpResponse.json(shareView()); }),
    );
    const { user } = renderShare();

    await screen.findByText("Accept to start editing");
    await user.click(screen.getByRole("button", { name: /Accept & continue/ }));

    expect(alertSpy).toHaveBeenCalledWith("Please enter your name.");
    expect(claimed).toBe(false);
    alertSpy.mockRestore();
  });

  it("recovers from a racing claim: re-reads the share and shows the note if no longer locked", async () => {
    let getCalls = 0;
    server.use(
      http.get(TOKEN_PATH, () => {
        getCalls += 1;
        // First mount: needs claim. After a failing claim, the re-read shows it unlocked
        // (another tab in this browser claimed it first, setting the cookie).
        return getCalls === 1
          ? HttpResponse.json(shareView({ requires_claim: true, note: undefined }))
          : HttpResponse.json(shareView());
      }),
      http.post("*/api/share/:token/claim", () =>
        HttpResponse.json({ detail: "locked" }, { status: 403 })),
    );
    const { user } = renderShare();

    await screen.findByText("Accept to view");
    await user.click(screen.getByRole("button", { name: /Accept & view/ }));

    // Despite the 403, the post-claim re-read finds it unlocked and renders the note.
    expect(await screen.findByRole("heading", { name: "Trip Plan", level: 1 })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (e) Propose-edit flow on an editable share (recipient suggests a change).
// ---------------------------------------------------------------------------- //
describe("SharePage — propose an edit", () => {
  it("editable note: edit → propose POSTs the new body + bound name and shows the sent state", async () => {
    let proposeBody: any = null;
    server.use(
      http.get(TOKEN_PATH, () =>
        HttpResponse.json(shareView({ can_edit: true, scope: "edit", bound_name: "Pat" }))),
      http.post("*/api/share/:token/propose", async ({ request }) => {
        proposeBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderShare();

    await screen.findByRole("heading", { name: "Trip Plan", level: 1 });
    await user.click(screen.getByRole("button", { name: /Suggest an edit/ }));

    // The editor textarea is seeded with the current markdown; rewrite it.
    const area = await screen.findByDisplayValue("Pack **socks** and a map.") as HTMLTextAreaElement;
    await user.clear(area);
    await user.type(area, "Bring sandals.");
    await user.type(screen.getByPlaceholderText(/Note to owner/), "fixed it");
    await user.click(screen.getByRole("button", { name: /Propose changes/ }));

    await waitFor(() => expect(proposeBody).not.toBeNull());
    expect(proposeBody).toEqual({ content_md: "Bring sandals.", name: "Pat", note: "fixed it" });
    // Confirmation state (no re-prompt for name since bound_name was present).
    expect(await screen.findByText("Your edit was sent for approval.")).toBeInTheDocument();
  });

  it("surfaces a propose error via alert and keeps the editor open", async () => {
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => {});
    server.use(
      http.get(TOKEN_PATH, () =>
        HttpResponse.json(shareView({ can_edit: true, scope: "edit", bound_name: "Pat" }))),
      http.post("*/api/share/:token/propose", () =>
        HttpResponse.json({ detail: "Server rejected the edit" }, { status: 500 })),
    );
    const { user } = renderShare();

    await screen.findByRole("heading", { name: "Trip Plan", level: 1 });
    await user.click(screen.getByRole("button", { name: /Suggest an edit/ }));
    await user.click(screen.getByRole("button", { name: /Propose changes/ }));

    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith("Server rejected the edit"));
    // Editor stays open so the proposal isn't lost.
    expect(screen.getByRole("button", { name: /Propose changes/ })).toBeInTheDocument();
    alertSpy.mockRestore();
  });
});
