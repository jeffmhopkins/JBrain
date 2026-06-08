// Component-integration (Tier 2) tests for ListEditor — the shared list-card editor.
// Props-only and pure (no fetch, no router): it renders the Parsed model (header is
// ignored here; items + queue flag drive the UI) with interactive checkboxes, add,
// reorder (up/down), remove, and an optional priority-queue toggle. It is controlled —
// every mutation computes the NEXT Parsed and hands it to onChange; the parent persists.
// We assert the rendered rows and the exact Parsed emitted by each interaction.
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup } from "../test/render";
import userEvent from "@testing-library/user-event";
import type { Parsed } from "../lists";

import ListEditor from "./ListEditor";

afterEach(cleanup);

function parsed(o: Partial<Parsed> = {}): Parsed {
  return {
    header: "",
    queue: false,
    items: [
      { checked: false, text: "Milk", priority: null },
      { checked: true, text: "Eggs", priority: null },
    ],
    ...o,
  };
}

describe("ListEditor — render", () => {
  it("renders each item's text and reflects its checked state", () => {
    render(<ListEditor value={parsed()} onChange={() => {}} />);
    expect(screen.getByText("Milk")).toBeInTheDocument();
    expect(screen.getByText("Eggs")).toBeInTheDocument();
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes[0]).not.toBeChecked();
    expect(boxes[1]).toBeChecked();
  });

  it("shows the empty-list placeholder when there are no items", () => {
    render(<ListEditor value={parsed({ items: [] })} onChange={() => {}} />);
    expect(screen.getByText(/Empty list\./i)).toBeInTheDocument();
  });

  it("renders a custom renderItemText when supplied", () => {
    render(
      <ListEditor
        value={parsed({ items: [{ checked: false, text: "raw", priority: null }] })}
        onChange={() => {}}
        renderItemText={(t) => <em data-testid="custom">{t.toUpperCase()}</em>}
      />,
    );
    expect(screen.getByTestId("custom")).toHaveTextContent("RAW");
  });

  it("shows P-badges from item priority, but position badges in queue mode", () => {
    const { rerender } = render(
      <ListEditor
        value={parsed({ items: [{ checked: false, text: "x", priority: 3 }] })}
        onChange={() => {}}
      />,
    );
    expect(screen.getByText("P3")).toBeInTheDocument();
    // Queue mode numbers items by position (top = 1), ignoring stored priority.
    rerender(
      <ListEditor
        value={parsed({ queue: true, items: [{ checked: false, text: "x", priority: 3 }] })}
        onChange={() => {}}
      />,
    );
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.queryByText("P3")).not.toBeInTheDocument();
  });
});

describe("ListEditor — interactions", () => {
  it("toggling a checkbox emits the updated item via onChange", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ListEditor value={parsed()} onChange={onChange} />);
    await user.click(screen.getAllByRole("checkbox")[0]);
    expect(onChange).toHaveBeenCalledTimes(1);
    const next: Parsed = onChange.mock.calls[0][0];
    expect(next.items[0].checked).toBe(true);
    // Other items untouched.
    expect(next.items[1]).toMatchObject({ text: "Eggs", checked: true });
  });

  it("adds a typed item on the Add button (and clears the input)", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ListEditor value={parsed()} onChange={onChange} />);
    const input = screen.getByPlaceholderText(/Add an item…/);
    await user.type(input, "  Butter  ");
    await user.click(screen.getByRole("button", { name: "Add" }));
    const next: Parsed = onChange.mock.calls.at(-1)![0];
    // Trimmed and appended, unchecked, no priority.
    expect(next.items.at(-1)).toMatchObject({ text: "Butter", checked: false, priority: null });
    expect(input).toHaveValue("");
  });

  it("adds an item on Enter and ignores a blank/whitespace add", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ListEditor value={parsed()} onChange={onChange} />);
    const input = screen.getByPlaceholderText(/Add an item…/);
    await user.type(input, "Bread{Enter}");
    expect(onChange.mock.calls.at(-1)![0].items.at(-1)).toMatchObject({ text: "Bread" });
    onChange.mockClear();
    // Whitespace-only add is a no-op.
    await user.type(input, "   {Enter}");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("removes an item via its ✕ button", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ListEditor value={parsed()} onChange={onChange} />);
    await user.click(screen.getAllByRole("button", { name: "✕" })[0]);
    const next: Parsed = onChange.mock.calls[0][0];
    expect(next.items).toHaveLength(1);
    expect(next.items[0].text).toBe("Eggs");
  });

  it("reorders with the up/down chevrons (first row's up is disabled)", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ListEditor value={parsed()} onChange={onChange} />);
    const ups = screen.getAllByRole("button", { name: "Move up" });
    const downs = screen.getAllByRole("button", { name: "Move down" });
    // Endpoints are disabled.
    expect(ups[0]).toBeDisabled();
    expect(downs[1]).toBeDisabled();
    // Moving the first item down swaps it with the second.
    await user.click(downs[0]);
    const next: Parsed = onChange.mock.calls[0][0];
    expect(next.items.map((i) => i.text)).toEqual(["Eggs", "Milk"]);
  });

  it("toggles the priority-queue flag when the toggle is shown", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ListEditor value={parsed({ queue: false })} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: /Priority queue/ }));
    expect(onChange.mock.calls[0][0].queue).toBe(true);
  });

  it("hides the queue toggle when showQueueToggle is false", () => {
    render(<ListEditor value={parsed()} onChange={() => {}} showQueueToggle={false} />);
    expect(screen.queryByRole("button", { name: /Priority queue/ })).not.toBeInTheDocument();
  });
});
