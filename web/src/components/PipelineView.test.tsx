// Component-integration (Tier 2) tests for PipelineView — the read-only visualisation
// of an action recipe's step pipeline. Props-only and pure (no fetch, no router): it
// renders from the parsed step tree the server returns (no client YAML parsing).
//   - a step shows its `do` verb and, if present, a "→ id" output chip
//   - {{ ... }} references inside string values render as .ref-chip chips
//   - guards (when / stop_when / stop_when_empty) render as .guard-pill rows
//   - `with` inputs render key + value; object values stringify to JSON <code>
//   - a for_each step is a .step-loop container with indented child StepNodes
import { afterEach, describe, expect, it } from "vitest";
import { render, screen, cleanup } from "../test/render";

import PipelineView from "./PipelineView";

afterEach(cleanup);

describe("PipelineView", () => {
  it("renders the empty-state when there are no steps", () => {
    const { rerender } = render(<PipelineView steps={[]} />);
    expect(screen.getByText(/No steps\./i)).toBeInTheDocument();
    // Also tolerant of a null/undefined steps prop.
    rerender(<PipelineView steps={undefined as any} />);
    expect(screen.getByText(/No steps\./i)).toBeInTheDocument();
  });

  it("renders a step's `do` verb and its `→ id` output chip", () => {
    const { container } = render(
      <PipelineView steps={[{ do: "search.notes", id: "hits" }]} />,
    );
    expect(screen.getByText("search.notes")).toBeInTheDocument();
    const out = container.querySelector(".out-chip");
    expect(out).not.toBeNull();
    expect(out!.textContent).toContain("hits");
  });

  it("falls back to an em-dash when a step has no `do`", () => {
    const { container } = render(<PipelineView steps={[{}]} />);
    expect(container.querySelector(".step-do")?.textContent).toBe("—");
  });

  it("splits {{ ... }} references in a `with` value into ref chips and plain text", () => {
    const { container } = render(
      <PipelineView steps={[{ do: "ai.ask", with: { prompt: "Summarise {{ hits.text }} now" } }]} />,
    );
    // The `with` key is shown…
    expect(screen.getByText("prompt")).toBeInTheDocument();
    // …and the {{ hits.text }} reference renders as a trimmed ref-chip.
    const chips = [...container.querySelectorAll(".ref-chip")];
    expect(chips).toHaveLength(1);
    expect(chips[0].textContent).toBe("hits.text");
    // The surrounding literal text is preserved.
    expect(screen.getByText(/Summarise/)).toBeInTheDocument();
    expect(screen.getByText(/now/)).toBeInTheDocument();
  });

  it("renders guard pills for when / stop_when / stop_when_empty", () => {
    const { container } = render(
      <PipelineView
        steps={[{ do: "x", when: "{{ flag }}", stop_when: "done", stop_when_empty: "hits" }]}
      />,
    );
    const guards = [...container.querySelectorAll(".guard-pill")].map((g) => g.textContent);
    expect(guards.some((t) => t?.startsWith("when:"))).toBe(true);
    expect(guards.some((t) => t?.startsWith("stop when:"))).toBe(true);
    expect(guards.some((t) => t?.startsWith("stop if empty:"))).toBe(true);
  });

  it("stringifies an object `with` value into a JSON <code> block", () => {
    const { container } = render(
      <PipelineView steps={[{ do: "x", with: { opts: { a: 1, b: 2 } } }]} />,
    );
    const code = container.querySelector("code");
    expect(code).not.toBeNull();
    expect(code!.textContent).toBe(JSON.stringify({ a: 1, b: 2 }));
  });

  it("renders a for_each step as a loop container with indented child steps", () => {
    const { container } = render(
      <PipelineView
        steps={[
          {
            for_each: "{{ hits }}",
            id: "loop_out",
            steps: [{ do: "child.one" }, { do: "child.two" }],
          },
        ]}
      />,
    );
    expect(container.querySelector(".step-loop")).not.toBeNull();
    expect(screen.getByText("for each")).toBeInTheDocument();
    // The loop's iterable reference is a ref-chip.
    expect(container.querySelector(".step-loop .ref-chip")?.textContent).toBe("hits");
    // Children render inside .step-children.
    const children = container.querySelectorAll(".step-children .step-card");
    expect(children).toHaveLength(2);
    expect(screen.getByText("child.one")).toBeInTheDocument();
    expect(screen.getByText("child.two")).toBeInTheDocument();
  });

  it("renders every top-level step in order", () => {
    const { container } = render(
      <PipelineView steps={[{ do: "first" }, { do: "second" }, { do: "third" }]} />,
    );
    const dos = [...container.querySelectorAll(".pipeline > .step-card .step-do")].map((e) => e.textContent);
    expect(dos).toEqual(["first", "second", "third"]);
  });
});
