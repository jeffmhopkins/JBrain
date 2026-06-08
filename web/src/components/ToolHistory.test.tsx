// Component-integration (Tier 2) tests for <ToolHistory> — the "How I answered this"
// per-reply tool-call timeline. The pill shows the step count; opening it lazily fetches
// getMessageSteps() (GET /api/chat/messages/:id/steps) and renders one row per tool call,
// each expandable to show its (pretty-printed) input args and verbatim result text.
//
// The pure parsing/labeling helpers (toolHistoryParse / toolLabels) are tested in their own
// suites; here we assert the rendered DOM and the open/collapse + lazy-load behaviour.
//
// Network: the only request the component makes is the steps GET, mocked per-test via MSW
// (onUnhandledRequest:"error"). The citation chips fetch a preview ONLY on hover, so tests
// never hover and thus fire no extra requests.
import { afterEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { renderWithProviders, screen, waitFor, cleanup } from "../test/render";
import { server } from "../test/server";
import type { MessageStep } from "../api";
import ToolHistory from "./ToolHistory";

function step(over: Partial<MessageStep> = {}): MessageStep {
  return {
    step_index: 0,
    tool_name: "search_notes",
    args_json: "{}",
    result_text: "",
    is_error: 0,
    event_json: null,
    ...over,
  };
}

// Register the steps GET for a given messageId, returning the supplied rows.
function stepsHandler(messageId: number, steps: MessageStep[]) {
  return http.get(`*/api/chat/messages/${messageId}/steps`, () => HttpResponse.json(steps));
}

const navigate = vi.fn();
afterEach(() => {
  cleanup();
  navigate.mockReset();
});

describe("ToolHistory — pill", () => {
  it("renders the pluralized count and does not fetch while collapsed", () => {
    // No steps handler registered: a stray fetch would fail (onUnhandledRequest:"error").
    renderWithProviders(
      <ToolHistory messageId={1} count={3} open={false} onToggle={() => {}} navigate={navigate} />,
    );
    expect(screen.getByText("3 steps")).toBeInTheDocument();
    // Panel is closed: no step list / loading text.
    expect(screen.queryByText("Loading…")).not.toBeInTheDocument();
  });

  it("uses the singular 'step' for a count of 1", () => {
    renderWithProviders(
      <ToolHistory messageId={1} count={1} open={false} onToggle={() => {}} navigate={navigate} />,
    );
    expect(screen.getByText("1 step")).toBeInTheDocument();
  });

  it("invokes onToggle when the pill is clicked", async () => {
    const onToggle = vi.fn();
    const { user } = renderWithProviders(
      <ToolHistory messageId={1} count={2} open={false} onToggle={onToggle} navigate={navigate} />,
    );
    await user.click(screen.getByRole("button", { name: /2 steps/i }));
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});

describe("ToolHistory — opened panel", () => {
  it("lazy-loads and renders a row per step with friendly (done) labels and tool names", async () => {
    server.use(
      stepsHandler(5, [
        step({ step_index: 0, tool_name: "search_notes", args_json: '{"q":"sleep"}', result_text: "found 2 notes" }),
        step({ step_index: 1, tool_name: "read_note", args_json: "{}", result_text: "the note body" }),
      ]),
    );
    renderWithProviders(
      <ToolHistory messageId={5} count={2} open onToggle={() => {}} navigate={navigate} />,
    );

    // Settled labels (no trailing ellipsis) from toolLabels.
    await screen.findByText("Searching your notes");
    expect(screen.getByText("Reading a note")).toBeInTheDocument();
    // Raw tool names shown alongside.
    expect(screen.getByText("search_notes")).toBeInTheDocument();
    expect(screen.getByText("read_note")).toBeInTheDocument();
    // Two collapsed step rows.
    expect(document.querySelectorAll("li.th-step")).toHaveLength(2);
  });

  it("falls back to 'Working' for an unknown tool kind", async () => {
    server.use(stepsHandler(6, [step({ tool_name: "totally_made_up" })]));
    renderWithProviders(
      <ToolHistory messageId={6} count={1} open onToggle={() => {}} navigate={navigate} />,
    );
    await screen.findByText("Working");
    expect(screen.getByText("totally_made_up")).toBeInTheDocument();
  });

  it("expands a step to reveal pretty-printed input args + result, then collapses again", async () => {
    server.use(
      stepsHandler(7, [step({ tool_name: "query_sql", args_json: '{"sql":"select 1"}', result_text: "row=1" })]),
    );
    const { user } = renderWithProviders(
      <ToolHistory messageId={7} count={1} open onToggle={() => {}} navigate={navigate} />,
    );

    const head = await screen.findByRole("button", { name: /Querying the database/i });
    expect(head).toHaveAttribute("aria-expanded", "false");
    // Collapsed: no Input/Result detail yet.
    expect(screen.queryByText("Result")).not.toBeInTheDocument();

    await user.click(head);
    expect(head).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Input")).toBeInTheDocument();
    // prettyArgs JSON-pretty-prints the args object.
    expect(screen.getByText(/"sql": "select 1"/)).toBeInTheDocument();
    expect(screen.getByText("Result")).toBeInTheDocument();
    expect(screen.getByText("row=1")).toBeInTheDocument();

    await user.click(head);
    expect(head).toHaveAttribute("aria-expanded", "false");
    await waitFor(() => expect(screen.queryByText("Result")).not.toBeInTheDocument());
  });

  it("shows '(no output)' and hides the Input section when args are empty", async () => {
    server.use(stepsHandler(8, [step({ tool_name: "list_tags", args_json: "{}", result_text: "" })]));
    const { user } = renderWithProviders(
      <ToolHistory messageId={8} count={1} open onToggle={() => {}} navigate={navigate} />,
    );

    const head = await screen.findByRole("button", { name: /Listing tags/i });
    await user.click(head);
    // Empty {} args => prettyArgs returns null => no Input header.
    expect(screen.queryByText("Input")).not.toBeInTheDocument();
    expect(screen.getByText("Result")).toBeInTheDocument();
    expect(screen.getByText("(no output)")).toBeInTheDocument();
  });

  it("marks an errored step with the failed badge and error class", async () => {
    server.use(stepsHandler(9, [step({ tool_name: "read_note", is_error: 1, result_text: "boom" })]));
    renderWithProviders(
      <ToolHistory messageId={9} count={1} open onToggle={() => {}} navigate={navigate} />,
    );
    await screen.findByText("failed");
    expect(document.querySelector("li.th-step-err")).not.toBeNull();
  });

  it("renders the event summary line and clickable note-ref chips", async () => {
    server.use(
      stepsHandler(10, [
        step({
          tool_name: "propose_actions",
          result_text: "see [[Sleep hygiene]] for details",
          event_json: JSON.stringify({ type: "staging", actions: [{}, {}] }),
        }),
      ]),
    );
    renderWithProviders(
      <ToolHistory messageId={10} count={1} open onToggle={() => {}} navigate={navigate} />,
    );

    // eventSummary() renders the staged-changes line.
    await screen.findByText(/Staged 2 change\(s\) for review/);
    // noteRefs() lifts the wikilink into a clickable chip.
    expect(screen.getByRole("link", { name: "Sleep hygiene" })).toBeInTheDocument();
  });

  it("shows the empty state when no tool calls were recorded", async () => {
    server.use(stepsHandler(11, []));
    renderWithProviders(
      <ToolHistory messageId={11} count={0} open onToggle={() => {}} navigate={navigate} />,
    );
    await screen.findByText("No tool calls recorded.");
  });

  it("shows the error message when the steps fetch fails", async () => {
    server.use(http.get("*/api/chat/messages/12/steps", () => HttpResponse.json({}, { status: 500 })));
    renderWithProviders(
      <ToolHistory messageId={12} count={2} open onToggle={() => {}} navigate={navigate} />,
    );
    await screen.findByText(/Couldn’t load the history\./);
  });
});
