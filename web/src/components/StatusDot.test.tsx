// Component-render test for the status indicator (jsdom). Mocks ../App so the heavy
// page-import graph isn't pulled in; drives the REAL health store and asserts the dot
// colour + panel rows actually render.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("./App", () => ({ useAuth: () => ({ brainName: "Test Brain" }) }));

// Stub only the resetAi recovery call; keep the rest of ../api real (setAccessKey, etc.).
const { resetAiMock } = vi.hoisted(() => ({
  resetAiMock: vi.fn(async () => ({ ok: true, clients_dropped: 1, runs_cancelled: 0 })),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  resetAi: resetAiMock,
}));

import StatusDot from "./StatusDot";
import { __reset, applyPoll, ingestVerify, setOnline } from "../health";
import { setAccessKey, clearAccessKey } from "../api";

function fullCaps(over: Record<string, any> = {}): any {
  return { capabilities: {
    llm: { state: "ready", verified: null, providers: { anthropic: true, xai: false } },
    embeddings: { state: "ready" }, transcription: { state: "ready" },
    push: { state: "ready" }, geocoder: { state: "ready" }, db: { state: "ready" },
    ...over,
  } };
}

beforeEach(() => { clearAccessKey(); __reset(); });
afterEach(() => cleanup());

function dotLevel(container: HTMLElement): string {
  const dot = container.querySelector(".status-dot-btn .status-dot")!;
  return dot.className.replace("status-dot", "").trim();
}

describe("StatusDot", () => {
  it("renders green when all subsystems are ready", () => {
    const { container } = render(<StatusDot />);
    act(() => { ingestVerify(fullCaps()); });
    expect(dotLevel(container)).toBe("ok");
  });

  it("renders red when a subsystem failed", () => {
    const { container } = render(<StatusDot />);
    act(() => { ingestVerify(fullCaps({ db: { state: "failed", last_error: "locked" } })); });
    expect(dotLevel(container)).toBe("down");
  });

  it("renders amber when the LLM is absent, and the panel explains why", () => {
    const { container } = render(<StatusDot />);
    act(() => { ingestVerify(fullCaps({ llm: { state: "absent", providers: { anthropic: false, xai: false } } })); });
    expect(dotLevel(container)).toBe("warn");
    fireEvent.click(container.querySelector(".status-dot-btn")!);   // open the panel
    expect(screen.getByText(/no API key configured/i)).toBeInTheDocument();
  });

  it("renders grey + 'offline' summary when the browser is offline", () => {
    const { container } = render(<StatusDot />);
    act(() => { ingestVerify(fullCaps()); setOnline(false); });
    expect(dotLevel(container)).toBe("unknown");
    fireEvent.click(container.querySelector(".status-dot-btn")!);
    expect(screen.getByText(/offline/i)).toBeInTheDocument();
  });

  it("'Reset AI' triggers the recovery endpoint", async () => {
    resetAiMock.mockClear();
    const { container } = render(<StatusDot />);
    act(() => { ingestVerify(fullCaps()); });
    fireEvent.click(container.querySelector(".status-dot-btn")!);   // open the panel
    fireEvent.click(screen.getByRole("button", { name: /Reset AI/i }));
    await waitFor(() => expect(resetAiMock).toHaveBeenCalledTimes(1));
  });

  it("renders amber 'Re-authenticate' on a rotated key (skeleton + stored key)", () => {
    setAccessKey("stored-but-rotated");
    const { container } = render(<StatusDot />);
    act(() => { applyPoll({ ok: true, data: { ok: true, brain: "Test Brain", ts: "x" } }); });
    expect(dotLevel(container)).toBe("warn");
    fireEvent.click(container.querySelector(".status-dot-btn")!);
    expect(screen.getByText(/re-authenticate/i)).toBeInTheDocument();
  });
});
