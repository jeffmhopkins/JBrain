// Component-integration (Tier 2) tests for OwnerOnboarding.tsx — the first-run gate
// that captures who owns the brain. The owner's name anchors first-person notes and
// titles their kb/People/<name> page; until it's set (or skipped on this device) this
// screen is shown.
//
// Network: the only endpoint the page touches goes through MSW (setup.ts runs
// onUnhandledRequest:"error", so the route must be stubbed):
//   PUT /api/people/owner  — save the owner name (setOwner(name) → body { name })
// The PUT body is recorded so routing + payload can be asserted. OwnerOnboarding does
// not import useAuth, but ../App is stubbed defensively (mirrors the sibling page tests)
// so the auth/router App tree is never dragged in. No production code is touched.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, fireEvent, cleanup } from "../test/render";
import { server } from "../test/server";

vi.mock("../App", () => ({ useAuth: () => ({ appTz: "UTC" }), PWA_VERSION: "test" }));

import OwnerOnboarding from "./OwnerOnboarding";

// Records every PUT body so we can assert routing + payload (and the count, to prove
// no request fires on the empty-name / skip paths).
let putOwner: any[] = [];

function ownerHandler(impl?: Parameters<typeof http.put>[1]) {
  server.use(
    http.put(
      "*/api/people/owner",
      impl ??
        (async ({ request }) => {
          const body: any = await request.json();
          putOwner.push(body);
          return HttpResponse.json({
            id: 1,
            name: body.name,
            display: body.name,
            is_set: true,
          });
        }),
    ),
  );
}

beforeEach(() => {
  putOwner = [];
  localStorage.clear();
});
afterEach(() => cleanup());

// ---------------------------------------------------------------------------- //
// (a) render
// ---------------------------------------------------------------------------- //
describe("OwnerOnboarding — render", () => {
  it("renders the welcome form with the brain name and the action buttons", () => {
    renderWithProviders(<OwnerOnboarding brainName="JBrain" onDone={() => {}} />);

    expect(screen.getByRole("heading", { name: /Welcome to JBrain/i })).toBeInTheDocument();
    expect(screen.getByText(/Who owns this brain\?/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/e\.g\. Jeff Hopkins/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save & continue/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /I.ll do this later/i })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) save
// ---------------------------------------------------------------------------- //
describe("OwnerOnboarding — save", () => {
  it("PUTs the trimmed name and calls onDone on success", async () => {
    ownerHandler();
    const onDone = vi.fn();
    renderWithProviders(<OwnerOnboarding brainName="JBrain" onDone={onDone} />);

    // fireEvent.change for the controlled input (modal focus-steal guidance).
    fireEvent.change(screen.getByPlaceholderText(/e\.g\. Jeff Hopkins/i), {
      target: { value: "  Jeff Hopkins  " },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save & continue/i }));

    await waitFor(() => expect(putOwner).toHaveLength(1));
    // setOwner(name) → { name } only (aliases undefined); name is trimmed by save().
    expect(putOwner[0]).toEqual({ name: "Jeff Hopkins" });
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
  });

  it("shows a 'Saving…' busy label on the submit button while in flight", async () => {
    // Block the response so the busy state is observable.
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    ownerHandler(async ({ request }) => {
      const body: any = await request.json();
      putOwner.push(body);
      await gate;
      return HttpResponse.json({ id: 1, name: body.name, display: body.name, is_set: true });
    });
    const onDone = vi.fn();
    renderWithProviders(<OwnerOnboarding brainName="JBrain" onDone={onDone} />);

    fireEvent.change(screen.getByPlaceholderText(/e\.g\. Jeff Hopkins/i), {
      target: { value: "Jeff" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save & continue/i }));

    const busyBtn = await screen.findByRole("button", { name: /Saving/i });
    expect(busyBtn).toBeDisabled();
    expect(onDone).not.toHaveBeenCalled();

    release();
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
  });
});

// ---------------------------------------------------------------------------- //
// (c) validation — empty name blocks
// ---------------------------------------------------------------------------- //
describe("OwnerOnboarding — validation", () => {
  it("does not save or advance when the name is empty/whitespace", async () => {
    ownerHandler();
    const onDone = vi.fn();
    renderWithProviders(<OwnerOnboarding brainName="JBrain" onDone={onDone} />);

    // Submitting blank: save() returns early (n falsy), no request, no onDone.
    fireEvent.click(screen.getByRole("button", { name: /Save & continue/i }));
    fireEvent.change(screen.getByPlaceholderText(/e\.g\. Jeff Hopkins/i), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save & continue/i }));

    // Give any (unwanted) request a chance to land before asserting none did.
    await Promise.resolve();
    expect(putOwner).toHaveLength(0);
    expect(onDone).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------- //
// (d) skip / dismiss
// ---------------------------------------------------------------------------- //
describe("OwnerOnboarding — skip", () => {
  it("'I'll do this later' records the skip flag and calls onDone without a PUT", async () => {
    ownerHandler();
    const onDone = vi.fn();
    renderWithProviders(<OwnerOnboarding brainName="JBrain" onDone={onDone} />);

    fireEvent.click(screen.getByRole("button", { name: /I.ll do this later/i }));

    expect(onDone).toHaveBeenCalledTimes(1);
    expect(putOwner).toHaveLength(0);
    expect(localStorage.getItem("jbrain_owner_setup_skipped")).toBe("1");
  });
});

// ---------------------------------------------------------------------------- //
// (e) error path
// ---------------------------------------------------------------------------- //
describe("OwnerOnboarding — error", () => {
  it("surfaces the server error message and does NOT call onDone when the save fails", async () => {
    ownerHandler(() => HttpResponse.json({ detail: "Owner locked" }, { status: 409 }));
    const onDone = vi.fn();
    renderWithProviders(<OwnerOnboarding brainName="JBrain" onDone={onDone} />);

    fireEvent.change(screen.getByPlaceholderText(/e\.g\. Jeff Hopkins/i), {
      target: { value: "Jeff" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save & continue/i }));

    // The ApiError message (detail) is rendered inline; onDone never fires.
    expect(await screen.findByText("Owner locked")).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
    // The button returns to its idle (enabled) state after the failure.
    expect(screen.getByRole("button", { name: /Save & continue/i })).not.toBeDisabled();
  });
});
