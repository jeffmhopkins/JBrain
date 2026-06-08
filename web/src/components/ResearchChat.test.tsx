// Component-integration (Tier 2) tests for ResearchChat.tsx — the recipient's view
// of a research Q&A link: a consent landing (name gate), a read-only Q&A interview
// over a scope the owner approved, optional inline lab charts the AI surfaces on
// demand, and an "ended" completion state.
//
// Network: every research endpoint is a plain JSON POST through publicApi (see api.ts:
// researchStart / researchTurn), so — like GuidedChat.test.tsx, not the SSE-mocking
// Chat.test.tsx — these tests drive the *real* api module entirely through MSW
// (server.use per test). There is no streaming/typewriter here: a turn's reply is the
// server's plain `message` JSON, rendered straight into the transcript.
//
// onUnhandledRequest:"error" (setup.ts) means every endpoint the component touches must
// be mocked. ResearchChat renders standalone (no health poll, no useAuth), so the only
// routes hit are research/start, research/turn, and — only when the AI surfaces a chart —
// research/labs/series (fetched by the inline <ShareChart variant="research">).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import ResearchChat from "./ResearchChat";

const TOKEN = "tok-123";

// Record each research POST (matched path + parsed body) so flows can assert routing
// and payloads without reaching into component internals.
let posted: { url: string; body: any }[] = [];

// A research endpoint that records the call and returns a scripted JSON turn. `path`
// is the suffix after the token (e.g. "start" | "turn").
function research(path: string, reply: (body: any) => any, status = 200) {
  return http.post(`*/api/share/:token/research/${path}`, async ({ request }) => {
    let body: any = null;
    try {
      body = await request.json();
    } catch {
      /* some requests send no body */
    }
    posted.push({ url: request.url, body });
    const out = reply(body);
    return HttpResponse.json(out ?? {}, { status });
  });
}

function urls(path: string) {
  return posted.filter((p) => p.url.includes(`/research/${path}`));
}

function renderResearch(over: Partial<React.ComponentProps<typeof ResearchChat>> = {}) {
  return renderWithProviders(
    <ResearchChat token={TOKEN} brainName="Test Brain" {...over} />,
  );
}

// Walk through the consent landing into the chat, with `start` returning the initial
// (usually empty) transcript. Returns the user-event handle.
async function begin(
  startReply: (body: any) => any = () => ({ transcript: [] }),
  opts: Partial<React.ComponentProps<typeof ResearchChat>> = {},
) {
  server.use(research("start", startReply));
  const { user } = renderResearch(opts);
  await user.type(screen.getByPlaceholderText(/Your name/i), "Alice");
  await user.click(screen.getByRole("button", { name: /Start/i }));
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
describe("ResearchChat — consent landing", () => {
  it("renders the heading, intro and read-only scope copy on the landing card", () => {
    renderResearch({ intro: "These are my recent records." });

    expect(
      screen.getByRole("heading", { name: /Ask Test Brain.s records/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/These are my recent records\./i)).toBeInTheDocument();
    // Read-only / scope-bounded posture is spelled out for the recipient.
    expect(
      screen.getByText(/only.*reads what they approved/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start/i })).toBeInTheDocument();
  });

  it("mentions the lab-chart capability only when the link has labs attached", () => {
    const { rerender } = renderResearch({ hasLabs: true });
    expect(screen.getByText(/pull up specific lab results/i)).toBeInTheDocument();

    rerender(<ResearchChat token={TOKEN} brainName="Test Brain" hasLabs={false} />);
    expect(screen.queryByText(/pull up specific lab results/i)).not.toBeInTheDocument();
  });

  it("blocks Start until a name is entered and never calls start", async () => {
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    server.use(research("start", () => ({ transcript: [] })));
    const { user } = renderResearch();
    await user.click(screen.getByRole("button", { name: /Start/i }));

    expect(alert).toHaveBeenCalledWith("Please enter your name.");
    expect(urls("start")).toHaveLength(0);
    alert.mockRestore();
  });
});

// ---------------------------------------------------------------------------- //
// (b) Begin → enter the chat (the initial render of the conversation)
// ---------------------------------------------------------------------------- //
describe("ResearchChat — begin the chat", () => {
  it("posts the name to /research/start and shows the empty-state ask prompt", async () => {
    await begin(() => ({ transcript: [] }));

    await waitFor(() => expect(urls("start")).toHaveLength(1));
    expect(urls("start")[0].body).toEqual({ name: "Alice" });

    // Empty conversation → the scoped ask hint, and the composer is shown.
    await screen.findByText(/Ask a question about the records Test Brain shared/i);
    expect(screen.getByPlaceholderText(/Ask a question/i)).toBeInTheDocument();
  });

  it("rehydrates a resumed session's transcript and maps roles to bubbles", async () => {
    await begin(() => ({
      transcript: [
        { role: "user", content: "Earlier question?" },
        { role: "assistant", content: "Earlier answer." },
      ],
    }));

    await screen.findByText(/Earlier question\?/i);
    expect(screen.getByText(/Earlier answer\./i)).toBeInTheDocument();
    // With messages present, the empty-state hint is gone.
    expect(
      screen.queryByText(/Ask a question about the records Test Brain shared/i),
    ).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (c) Asking a question → posts the message, renders the reply
// ---------------------------------------------------------------------------- //
describe("ResearchChat — asking a question", () => {
  it("posts the question to /research/turn and renders both the user turn and the answer", async () => {
    const user = await begin();
    await screen.findByPlaceholderText(/Ask a question/i);

    server.use(
      research("turn", () => ({ message: "Your last reading was normal." })),
    );
    const box = screen.getByPlaceholderText(/Ask a question/i);
    await user.type(box, "How are my labs?");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    await waitFor(() => expect(urls("turn")).toHaveLength(1));
    expect(urls("turn")[0].body).toEqual({ message: "How are my labs?" });

    // The optimistic user bubble and the AI answer both render.
    expect(screen.getByText("How are my labs?")).toBeInTheDocument();
    await screen.findByText(/Your last reading was normal\./i);
    // Composer is cleared after sending.
    expect((box as HTMLTextAreaElement).value).toBe("");
  });

  it("renders an inline lab chart when the AI surfaces one", async () => {
    const user = await begin();
    await screen.findByPlaceholderText(/Ask a question/i);

    server.use(
      research("turn", () => ({
        message: "Here is your glucose trend.",
        charts: [{ analyte: "glucose", unit: "mg/dL", from: "2024-01-01", to: "2024-12-31", title: "Glucose" }],
      })),
      // ShareChart(variant="research") fetches the token-scoped series. A no-points
      // series renders nothing, but the request MUST be mocked (onUnhandledRequest:error).
      http.get("*/api/share/:token/research/labs/series", ({ request }) => {
        posted.push({ url: request.url, body: null });
        return HttpResponse.json({
          analyte: "glucose", test_name: "Glucose", unit: "mg/dL",
          points: [], segments: [], domain: null, value_range: null,
          encounters: [], other_units: [],
        });
      }),
    );
    await user.type(screen.getByPlaceholderText(/Ask a question/i), "show glucose");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    await screen.findByText(/Here is your glucose trend\./i);
    // The inline ShareChart fetched its series through the research-scoped endpoint.
    await waitFor(() =>
      expect(posted.some((p) => p.url.includes("/research/labs/series"))).toBe(true),
    );
  });
});

// ---------------------------------------------------------------------------- //
// (d) Send gating — empty/whitespace input and the in-flight lock
// ---------------------------------------------------------------------------- //
describe("ResearchChat — send gating", () => {
  it("keeps Send disabled with empty input and posts no turn", async () => {
    const user = await begin();
    await screen.findByPlaceholderText(/Ask a question/i);

    const send = screen.getByRole("button", { name: /Send/i }) as HTMLButtonElement;
    expect(send.disabled).toBe(true); // nothing typed yet

    await user.type(screen.getByPlaceholderText(/Ask a question/i), "hi");
    expect(send.disabled).toBe(false);
    expect(urls("turn")).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------- //
// (e) Completion — the server-signalled "ended" phase hides the composer
// ---------------------------------------------------------------------------- //
describe("ResearchChat — completion", () => {
  it("ends the conversation and hides the composer when a turn returns phase 'ended'", async () => {
    const user = await begin();
    await screen.findByPlaceholderText(/Ask a question/i);

    server.use(
      research("turn", () => ({ message: "That's all I can share. Thanks!", phase: "ended" })),
    );
    await user.type(screen.getByPlaceholderText(/Ask a question/i), "anything else?");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    await screen.findByText(/That's all I can share\. Thanks!/i);
    await screen.findByText(/This conversation has ended/i);
    // No composer once the conversation is closed.
    expect(screen.queryByPlaceholderText(/Ask a question/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Send/i })).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (f) Error handling — a failed turn request and a failed start
// ---------------------------------------------------------------------------- //
describe("ResearchChat — errors", () => {
  it("surfaces a failed turn as an inline error and keeps the composer for retry", async () => {
    const user = await begin();
    await screen.findByPlaceholderText(/Ask a question/i);

    server.use(research("turn", () => ({ detail: "boom" }), 500));
    await user.type(screen.getByPlaceholderText(/Ask a question/i), "question");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    // publicApi throws ApiError("boom"); send() catches it into the inline error row.
    await screen.findByText(/boom/i);
    // The optimistic user bubble stays, and the composer remains for a retry.
    expect(screen.getByText("question")).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/Ask a question/i)).toBeInTheDocument();
  });

  it("stays on the consent landing (never enters the chat) when start fails", async () => {
    server.use(research("start", () => ({ detail: "link expired" }), 410));
    const { user } = renderResearch();
    await user.type(screen.getByPlaceholderText(/Your name/i), "Alice");
    await user.click(screen.getByRole("button", { name: /Start/i }));

    // The failed start is caught (the landing has no error row to render it), so the
    // recipient never advances into the chat and Start re-enables for a retry.
    await waitFor(() => expect(urls("start")).toHaveLength(1));
    const start = screen.getByRole("button", { name: /Start/i }) as HTMLButtonElement;
    await waitFor(() => expect(start.disabled).toBe(false));
    expect(screen.queryByPlaceholderText(/Ask a question/i)).not.toBeInTheDocument();
  });
});
