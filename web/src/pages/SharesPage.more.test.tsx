// Additional component-integration (Tier 2) tests for the Shares page, extending the
// coverage in SharesPage.test.tsx. These exercise the still-uncovered owner-side guided
// surfaces SharesPage renders DIRECTLY: the per-link GuidedEditor (edit brief + expiry),
// the lazy GuidedSessions list (view/delete a recipient conversation, error state), the
// guided link options (lock / single-use toggles, reset-lock), copy + revoke/delete, the
// pending-intake Discard and ended-chat Dismiss/distress flows, and the note-link Expiry
// prompt. Same conventions as the sibling file: ../App is stubbed, every endpoint is
// mocked (MSW onUnhandledRequest:"error"), queries are by role/label/text.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import { setAccessKey } from "../api";

vi.mock("../App", () => ({
  useAuth: () => ({ appTz: "UTC", brainName: "Test Brain" }),
  PWA_VERSION: "test",
}));

import SharesPage from "./SharesPage";

let posted: { url: string; method: string; body: any }[] = [];

function sharesPayload(over: Record<string, unknown> = {}) {
  return {
    links: [], proposals: [], history: [], guided_links: [], guided_pending: [],
    guided_ended: [], guided_history: [], research_links: [], lab_links: [], chat_links: [],
    ...over,
  };
}
function sharesHandlers(payload: Record<string, unknown> = {}) {
  return [http.get("*/api/shares", () => HttpResponse.json(sharesPayload(payload)))];
}

const GLINK = (over: Record<string, unknown> = {}) => ({
  id: 60, token: "g60", url: "https://x/share/g60", goal: "Onboarding interview", intro: "hello",
  sub_prompt: "ask about goals", spec_status: "active", bind: 0, single_use: 0, started: 0,
  note_title: "notes/Intake", note_slug: "intake", submitted: 0, expires_at: null, ...over,
});

beforeEach(() => {
  posted = [];
  setAccessKey("owner-access-key");
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true });
});
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// Guided link: edit the AI brief + expiry, copy the url, toggle options, revoke.
// ---------------------------------------------------------------------------- //
describe("SharesPage — guided link editor + options", () => {
  it("edits the AI brief + expiry and saves via guided details", async () => {
    let detailsBody: any = null;
    server.use(
      ...sharesHandlers({ guided_links: [GLINK({ id: 64 })] }),
      http.post("*/api/shares/guided/64/details", async ({ request }) => {
        detailsBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    // Expand the inline editor.
    await user.click(screen.getByRole("button", { name: /Edit the AI’s instructions/i }));
    const goal = await screen.findByLabelText(/^Goal$/i);
    expect(goal).toHaveValue("Onboarding interview");

    // Change the goal + the interview instructions, then Save.
    await user.clear(goal);
    await user.type(goal, "Revised goal");
    const sub = screen.getByLabelText(/Interview instructions/i);
    await user.clear(sub);
    await user.type(sub, "new brief");
    await user.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() => expect(detailsBody).not.toBeNull());
    expect(detailsBody.goal).toBe("Revised goal");
    expect(detailsBody.sub_prompt).toBe("new brief");
    expect(detailsBody).toHaveProperty("ttl_days");
    // Confirmation label appears after a successful save.
    await screen.findByText(/Saved/);
  });

  it("disables Save in the editor when the interview brief is empty", async () => {
    server.use(...sharesHandlers({ guided_links: [GLINK({ id: 65, sub_prompt: "" })] }));
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    await user.click(screen.getByRole("button", { name: /Edit the AI’s instructions/i }));
    await screen.findByLabelText(/Interview instructions/i);
    expect(screen.getByRole("button", { name: /^Save$/i })).toBeDisabled();
  });

  it("copies a live guided link url to the clipboard", async () => {
    server.use(...sharesHandlers({ guided_links: [GLINK({ id: 66 })] }));
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    await user.click(screen.getByRole("button", { name: /^Copy$/i }));
    await waitFor(async () => expect(await navigator.clipboard.readText()).toBe("https://x/share/g60"));
  });

  it("toggles the lock option (guided options) optimistically", async () => {
    let optBody: any = null;
    server.use(
      ...sharesHandlers({ guided_links: [GLINK({ id: 67, bind: 0, single_use: 1 })] }),
      http.post("*/api/shares/guided/67/options", async ({ request }) => {
        optBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    await user.click(screen.getByRole("checkbox", { name: /Lock to first browser/i }));
    await waitFor(() => expect(optBody).not.toBeNull());
    // The lock flips on; single-use (already on) is carried through unchanged.
    expect(optBody).toEqual({ bind: true, single_use: true });
  });

  it("resets the device lock on a started, bound guided link", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      ...sharesHandlers({ guided_links: [GLINK({ id: 68, bind: 1, started: 2 })] }),
      http.post("*/api/shares/guided/68/reset-bind", () => {
        posted.push({ url: "/api/shares/guided/68/reset-bind", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    await user.click(screen.getByRole("button", { name: /^Reset lock$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/guided/68/reset-bind"))).toBe(true));
  });

  it("revokes a live guided link (shared revoke POST) and drops it optimistically", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    // The page reloads /api/shares after revoking; mirror the server by dropping the
    // link once the revoke POST lands so the row stays gone.
    let revoked = false;
    server.use(
      http.get("*/api/shares", () =>
        HttpResponse.json(sharesPayload({ guided_links: revoked ? [] : [GLINK({ id: 69 })] }))),
      http.post("*/api/shares/69/revoke", () => {
        revoked = true;
        posted.push({ url: "/api/shares/69/revoke", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    await user.click(screen.getByRole("button", { name: /^Revoke$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/69/revoke"))).toBe(true));
    await waitFor(() => expect(screen.queryByText("Onboarding interview")).not.toBeInTheDocument());
  });
});

// ---------------------------------------------------------------------------- //
// Guided sessions: view a recipient transcript, delete one, and the load-error path.
// ---------------------------------------------------------------------------- //
describe("SharesPage — guided per-link sessions", () => {
  const SESSIONS = {
    sessions: [{
      id: 5, name: "Casey", status: "submitted", end_reason: null, turn_count: 3,
      created_at: "2026-01-01 00:00:00", completed_at: "2026-01-02 00:00:00",
      has_document: 1, has_transcript: 1,
    }],
  };

  it("loads a session transcript when a row is viewed", async () => {
    server.use(
      ...sharesHandlers({ guided_links: [GLINK({ id: 70, started: 1 })] }),
      http.get("*/api/shares/guided/70/sessions", () => HttpResponse.json(SESSIONS)),
      http.get("*/api/shares/guided/70/sessions/5", () => HttpResponse.json({
        transcript: [{ role: "assistant", content: "hi there" }, { role: "user", content: "my answer" }],
        document_md: "# Draft", note_slug: null,
      })),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    await user.click(screen.getByRole("button", { name: /View conversations \(1\)/i }));
    await screen.findByText("Casey");
    await user.click(screen.getByRole("button", { name: /^View$/i }));
    await screen.findByText("my answer");
  });

  it("deletes a session (guided delete) and refreshes the list", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let listCalls = 0;
    server.use(
      ...sharesHandlers({ guided_links: [GLINK({ id: 71, started: 1 })] }),
      http.get("*/api/shares/guided/71/sessions", () => {
        listCalls++;
        return HttpResponse.json(listCalls === 1 ? SESSIONS : { sessions: [] });
      }),
      http.get("*/api/shares/guided/71/sessions/5", () => HttpResponse.json({
        transcript: [{ role: "user", content: "hello" }], document_md: "", note_slug: null,
      })),
      http.delete("*/api/shares/guided/sessions/5", () => {
        posted.push({ url: "/api/shares/guided/sessions/5", method: "DELETE", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    await user.click(screen.getByRole("button", { name: /View conversations \(1\)/i }));
    await screen.findByText("Casey");
    await user.click(screen.getByRole("button", { name: /^View$/i }));
    await screen.findByText("hello");

    await user.click(screen.getByRole("button", { name: /Delete conversation/i }));
    await waitFor(() => expect(posted.some((p) => p.method === "DELETE")).toBe(true));
    // The refreshed list comes back empty.
    await screen.findByText(/No one has started this intake yet/i);
  });

  it("shows a friendly error when a session transcript fails to load", async () => {
    server.use(
      ...sharesHandlers({ guided_links: [GLINK({ id: 72, started: 1 })] }),
      http.get("*/api/shares/guided/72/sessions", () => HttpResponse.json(SESSIONS)),
      http.get("*/api/shares/guided/72/sessions/5", () => new HttpResponse(null, { status: 500 })),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Onboarding interview");

    await user.click(screen.getByRole("button", { name: /View conversations \(1\)/i }));
    await screen.findByText("Casey");
    await user.click(screen.getByRole("button", { name: /^View$/i }));
    await screen.findByText(/Couldn't load this conversation/i);
  });
});

// ---------------------------------------------------------------------------- //
// Inline pending/ended review actions: Discard a pending intake, Dismiss an ended
// chat, and the distress branch (no re-open offered, gentle copy + transcript view).
// ---------------------------------------------------------------------------- //
describe("SharesPage — pending discard + ended dismiss", () => {
  it("discards a pending intake (guided reject) and drops the card optimistically", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const pending = {
      id: 73, name: "Jordan", document_md: "# Summary", goal: "Health intake",
      note_title: "notes/Health", note_slug: "health", completed_at: "2026-01-03 00:00:00",
      transcript: [{ role: "user", content: "fine" }],
    };
    let discarded = false;
    server.use(
      http.get("*/api/shares", () =>
        HttpResponse.json(sharesPayload({ guided_pending: discarded ? [] : [pending] }))),
      http.post("*/api/shares/guided/sessions/73/reject", () => {
        discarded = true;
        posted.push({ url: "/api/shares/guided/sessions/73/reject", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText(/Health intake/);

    await user.click(screen.getByRole("button", { name: /^Discard$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/guided/sessions/73/reject"))).toBe(true));
    await waitFor(() => expect(screen.queryByText(/Health intake/)).not.toBeInTheDocument());
  });

  it("dismisses a distress-ended chat (guided acknowledge) — no re-open offered", async () => {
    const ended = {
      id: 74, name: "Sam", end_reason: "distress", goal: "Survey", note_title: "notes/Survey",
      note_slug: "survey", link_id: 74, link_status: "active", completed_at: "2026-01-04 00:00:00",
      transcript: [{ role: "assistant", content: "are you okay?" }, { role: "user", content: "struggling" }],
    };
    let dismissed = false;
    server.use(
      http.get("*/api/shares", () =>
        HttpResponse.json(sharesPayload({ guided_ended: dismissed ? [] : [ended] }))),
      http.post("*/api/shares/guided/sessions/74/acknowledge", () => {
        dismissed = true;
        posted.push({ url: "/api/shares/guided/sessions/74/acknowledge", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Survey");
    // Distress copy + badge; the link was NOT locked, so no Re-open button appears.
    expect(screen.getByText(/may need support/i)).toBeInTheDocument();
    expect(screen.getByText(/may have shared something difficult/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Re-open link/i })).not.toBeInTheDocument();

    // The transcript expands inline for review.
    await user.click(screen.getByRole("button", { name: /View conversation \(2\)/i }));
    await screen.findByText("struggling");

    await user.click(screen.getByRole("button", { name: /^Dismiss$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/guided/sessions/74/acknowledge"))).toBe(true));
    await waitFor(() => expect(screen.queryByText("Survey")).not.toBeInTheDocument());
  });
});

// ---------------------------------------------------------------------------- //
// Note-share link: the Expiry prompt flow (setLinkExpiry) + Reset-lock on a bound link.
// ---------------------------------------------------------------------------- //
describe("SharesPage — note-link expiry + reset-lock", () => {
  const NOTE_LINK = (over: Record<string, unknown> = {}) => ({
    id: 8, token: "tok8", scope: "view", label: null, created_at: "2026-01-01 00:00:00",
    last_used_at: null, expires_at: null, bind: 0, bound_at: null, pending: 0,
    note_title: "notes/Plan", note_slug: "plan", url: "https://x/share/tok8", ...over,
  });

  it("sets a link expiry through the prompt (shares/:id/expiry)", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("7");
    let expiryBody: any = null;
    server.use(
      ...sharesHandlers({ links: [NOTE_LINK()] }),
      http.post("*/api/shares/8/expiry", async ({ request }) => {
        expiryBody = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Plan");

    await user.click(screen.getByRole("button", { name: /^Expiry$/i }));
    await waitFor(() => expect(expiryBody).toEqual({ ttl_days: 7 }));
  });

  it("does nothing when the expiry prompt is cancelled", async () => {
    vi.spyOn(window, "prompt").mockReturnValue(null);
    server.use(...sharesHandlers({ links: [NOTE_LINK({ id: 18 })] }));
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Plan");

    // Cancelling the prompt must NOT hit the expiry endpoint (MSW would error otherwise).
    await user.click(screen.getByRole("button", { name: /^Expiry$/i }));
    expect(screen.getByText("Plan")).toBeInTheDocument();
  });

  it("resets the lock on a bound note link (shares/:id/reset-bind)", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    server.use(
      ...sharesHandlers({ links: [NOTE_LINK({ id: 19, bind: 1, bound_at: "2026-01-02 00:00:00" })] }),
      http.post("*/api/shares/19/reset-bind", () => {
        posted.push({ url: "/api/shares/19/reset-bind", method: "POST", body: null });
        return HttpResponse.json({ ok: true });
      }),
    );
    const { user } = renderWithProviders(<SharesPage />);
    await screen.findByText("Plan");
    // The "locked" badge surfaces because the link is bound + already opened once.
    expect(screen.getByText("locked")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Reset lock$/i }));
    await waitFor(() => expect(posted.some((p) => p.url.includes("/api/shares/19/reset-bind"))).toBe(true));
  });
});
