// Component-integration (Tier 2) tests for GuidedChat.tsx — the recipient's view
// of a guided AI intake link: a consent landing, a goal-driven Q&A interview, an
// optional review/summary step, and a final submit.
//
// Network: every guided endpoint is a plain JSON POST through publicApi (see api.ts:
// guidedStart / guidedTurn / guidedSubmit), so — unlike Chat.test.tsx, which mocks the
// SSE streamChat — these tests drive the *real* api module entirely through MSW
// (server.use per test). The "streaming" the component advertises is a client-side
// typewriter that reveals the server's full sanitized reply and then commits it to the
// transcript; with real timers, findText/waitFor simply wait for that reveal to finish.
//
// onUnhandledRequest:"error" (setup.ts) means every endpoint the component touches must
// be mocked. GuidedChat renders standalone (no health poll, no useAuth), so the only
// routes hit are the three guided endpoints — and only the ones a given flow exercises.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import GuidedChat from "./GuidedChat";

const TOKEN = "tok-123";

// Record each guided POST (matched path + parsed body) so flows can assert routing
// and payloads without reaching into component internals.
let posted: { url: string; body: any }[] = [];

// A guided endpoint that records the call and returns a scripted JSON turn. `path`
// is the suffix after the token (e.g. "start" | "turn" | "submit").
function guided(path: string, reply: (body: any) => any, status = 200) {
  return http.post(`*/api/share/:token/guided/${path}`, async ({ request }) => {
    let body: any = null;
    try {
      body = await request.json();
    } catch {
      /* submit sends no body */
    }
    posted.push({ url: request.url, body });
    const out = reply(body);
    return HttpResponse.json(out ?? {}, { status });
  });
}

function urls(path: string) {
  return posted.filter((p) => p.url.includes(`/guided/${path}`));
}

function renderGuided(over: Partial<React.ComponentProps<typeof GuidedChat>> = {}) {
  return renderWithProviders(
    <GuidedChat token={TOKEN} brainName="Test Brain" {...over} />,
  );
}

// Walk through the consent landing into the interview, with `start` returning the
// first scripted turn. Returns the user-event handle.
async function begin(
  startReply: (body: any) => any,
  opts: Partial<React.ComponentProps<typeof GuidedChat>> = {},
) {
  server.use(guided("start", startReply));
  const { user } = renderGuided(opts);
  await user.type(screen.getByPlaceholderText(/Your name/i), "Alice");
  await user.click(screen.getByRole("button", { name: /Begin/i }));
  return user;
}

beforeEach(() => {
  posted = [];
  // jsdom has no scrollIntoView; the component calls it in an effect on every
  // message/phase change. Stub as a no-op so those effects don't throw.
  if (!HTMLElement.prototype.scrollIntoView) {
    HTMLElement.prototype.scrollIntoView = () => {};
  }
});
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) Consent landing
// ---------------------------------------------------------------------------- //
describe("GuidedChat — consent landing", () => {
  it("renders the goal, intro and consent copy on the landing card", () => {
    renderGuided({
      goal: "share your medical history",
      intro: "We will ask a few questions.",
      consent: "Your answers go to the brain owner for review.",
    });
    // Goal is title-cased into the heading.
    expect(
      screen.getByRole("heading", { name: /Share your medical history/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/We will ask a few questions\./i)).toBeInTheDocument();
    expect(
      screen.getByText(/Your answers go to the brain owner for review\./i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Begin/i })).toBeInTheDocument();
  });

  it("blocks Begin until a name is entered and never calls start", async () => {
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    server.use(guided("start", () => ({ phase: "asking", message: "Q1?" })));
    const { user } = renderGuided();
    await user.click(screen.getByRole("button", { name: /Begin/i }));
    expect(alert).toHaveBeenCalledWith("Please enter your name.");
    expect(urls("start")).toHaveLength(0);
    alert.mockRestore();
  });
});

// ---------------------------------------------------------------------------- //
// (b) Begin → first question (the initial prompt render)
// ---------------------------------------------------------------------------- //
describe("GuidedChat — begin the interview", () => {
  it("posts the name to /guided/start and reveals the first question", async () => {
    await begin(() => ({ phase: "asking", message: "What brings you in today?" }));

    await waitFor(() => expect(urls("start")).toHaveLength(1));
    expect(urls("start")[0].body).toEqual({ name: "Alice" });

    // The first AI question types out, then the composer is shown.
    await screen.findByText(/What brings you in today\?/i);
    expect(screen.getByPlaceholderText(/Type your answer/i)).toBeInTheDocument();
  });

  it("rehydrates a resumed session's transcript instead of typing a new turn", async () => {
    await begin(() => ({
      resumed: true,
      phase: "asking",
      transcript: [
        { role: "assistant", content: "Earlier question?" },
        { role: "user", content: "Earlier answer." },
      ],
    }));

    await screen.findByText(/Earlier question\?/i);
    expect(screen.getByText(/Earlier answer\./i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (c) Answering a turn → posts the message, reveals the next question
// ---------------------------------------------------------------------------- //
describe("GuidedChat — answering a turn", () => {
  it("posts the answer to /guided/turn and renders both the user turn and the next question", async () => {
    const user = await begin(() => ({ phase: "asking", message: "First question?" }));
    await screen.findByText(/First question\?/i);

    server.use(guided("turn", () => ({ phase: "asking", message: "Second question?" })));
    const box = screen.getByPlaceholderText(/Type your answer/i);
    await user.type(box, "My answer");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    await waitFor(() => expect(urls("turn")).toHaveLength(1));
    expect(urls("turn")[0].body).toEqual({ message: "My answer" });

    // The optimistic user bubble and the streamed-in next question both render.
    expect(screen.getByText("My answer")).toBeInTheDocument();
    await screen.findByText(/Second question\?/i);
    // Composer is cleared after sending.
    expect((box as HTMLTextAreaElement).value).toBe("");
  });

  it("gates Send: empty input keeps it disabled, no turn is posted", async () => {
    const user = await begin(() => ({ phase: "asking", message: "Q?" }));
    await screen.findByText(/Q\?/i);

    const send = screen.getByRole("button", { name: /Send/i }) as HTMLButtonElement;
    expect(send.disabled).toBe(true); // nothing typed yet

    await user.type(screen.getByPlaceholderText(/Type your answer/i), "hi");
    expect(send.disabled).toBe(false);
    // Forcing a click with whitespace-only would no-op; here we just confirm the
    // empty-state guard never reached the network.
    expect(urls("turn")).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------- //
// (d) Review → submit, and the "ended"/closed completion state
// ---------------------------------------------------------------------------- //
describe("GuidedChat — completion", () => {
  it("shows the review summary, then submits to /guided/submit and confirms", async () => {
    const user = await begin(() => ({
      phase: "review",
      document: "## Summary\n\nAll your answers.",
      message: "Here is what I captured.",
    }));

    // The review document bubble and the recap message both render.
    await screen.findByText(/All your answers\./i);
    await screen.findByText(/Here is what I captured\./i);
    // Review composer offers send/add-more, not the answer textarea.
    expect(
      screen.queryByPlaceholderText(/Type your answer/i),
    ).not.toBeInTheDocument();

    server.use(guided("submit", () => ({ ok: true })));
    await user.click(screen.getByRole("button", { name: /Looks good — send/i }));

    await waitFor(() => expect(urls("submit")).toHaveLength(1));
    expect(urls("submit")[0].body).toBeNull(); // submit posts no body
    // The "sent" confirmation panel appears.
    await screen.findByText(/Thank you, Alice!/i);
    expect(screen.getByText(/were sent to Test Brain/i)).toBeInTheDocument();
  });

  it("reaches the closed/ended state with a closing message and hides the composer", async () => {
    await begin(() => ({ phase: "ended", message: "Thanks, we are all done." }));

    await screen.findByText(/Thanks, we are all done\./i);
    // No composer once the interview is closed.
    expect(
      screen.queryByPlaceholderText(/Type your answer/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Send/i })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (e) Error handling — in-turn error event and a failed request
// ---------------------------------------------------------------------------- //
describe("GuidedChat — errors", () => {
  it("renders an in-turn error phase from the server without crashing", async () => {
    const user = await begin(() => ({ phase: "asking", message: "Q?" }));
    await screen.findByText(/Q\?/i);

    server.use(
      guided("turn", () => ({ phase: "error", message: "The model is unavailable." })),
    );
    await user.type(screen.getByPlaceholderText(/Type your answer/i), "answer");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    await waitFor(() => expect(urls("turn")).toHaveLength(1));
    await screen.findByText(/The model is unavailable\./i);
    // The composer stays available so the recipient can retry.
    expect(screen.getByPlaceholderText(/Type your answer/i)).toBeInTheDocument();
  });

  it("surfaces a failed turn request as an inline error and keeps the composer", async () => {
    const user = await begin(() => ({ phase: "asking", message: "Q?" }));
    await screen.findByText(/Q\?/i);

    server.use(
      guided("turn", () => ({ detail: "boom" }), 500),
    );
    await user.type(screen.getByPlaceholderText(/Type your answer/i), "answer");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    // publicApi throws ApiError("boom"); send() catches it into the inline error row.
    await screen.findByText(/boom/i);
    expect(screen.getByPlaceholderText(/Type your answer/i)).toBeInTheDocument();
  });

  it("shows an error landing when start itself fails", async () => {
    server.use(guided("start", () => ({ detail: "link expired" }), 410));
    const { user } = renderGuided();
    await user.type(screen.getByPlaceholderText(/Your name/i), "Alice");
    await user.click(screen.getByRole("button", { name: /Begin/i }));

    await screen.findByText(/link expired/i);
  });
});
