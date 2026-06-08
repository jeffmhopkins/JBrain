// Component-integration (Tier 2) tests for ApprovalView.tsx — the single renderer for
// every kind of staged change (text/fields/tags/place). It is props-only: given a
// uniform `preview` { kind, before, after, ... } it renders the appropriate
// before/after view (a markdown diff for text, a chip set for tags, a was→now table
// for fields/place). before==null ⇒ create, after==null ⇒ delete.
//
// No network here — it's a pure presentational component. MarkdownDiff calls
// useNavigate(), so we still render under the router via renderWithProviders. We also
// exercise the exported helpers (lineDiff, previewStat) that StagingPanel consumes.
import { describe, expect, it } from "vitest";
import { renderWithProviders, screen, cleanup } from "../test/render";
import { afterEach } from "vitest";

import ApprovalView, { lineDiff, previewStat, type Preview } from "./ApprovalView";

afterEach(cleanup);

// ---------------------------------------------------------------------------- //
// (a) text diffs: create / edit / delete
// ---------------------------------------------------------------------------- //
describe("ApprovalView — text", () => {
  it("renders a create (before==null) as new body content, no delete banner", () => {
    const p: Preview = { kind: "text", before: null, after: "# Sourdough\n\nFeed the starter daily." };
    renderWithProviders(<ApprovalView preview={p} />);

    // The added body is shown.
    expect(screen.getByText(/Feed the starter daily/)).toBeInTheDocument();
    // No "will be deleted" banner on a create.
    expect(screen.queryByText(/will be deleted/i)).not.toBeInTheDocument();
  });

  it("renders an edit (both sides) showing before + after content via the diff", () => {
    const p: Preview = { kind: "text", before: "Milk\nEggs", after: "Milk\nEggs\nButter" };
    renderWithProviders(<ApprovalView preview={p} />);

    // The merged in-place diff surfaces both the kept and the newly-added lines.
    expect(screen.getByText(/Milk/)).toBeInTheDocument();
    expect(screen.getByText(/Butter/)).toBeInTheDocument();
    expect(screen.queryByText(/will be deleted/i)).not.toBeInTheDocument();
  });

  it("renders a delete (after==null) with the soft-delete banner and the old body", () => {
    const p: Preview = { kind: "text", before: "# Obsolete\n\nstale body", after: null };
    renderWithProviders(<ApprovalView preview={p} />);

    expect(screen.getByText(/This note will be deleted \(soft, undoable\)/i)).toBeInTheDocument();
    // The about-to-be-removed body still renders (struck-through).
    expect(screen.getByText(/stale body/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (b) tags
// ---------------------------------------------------------------------------- //
describe("ApprovalView — tags", () => {
  it("renders added (+), removed (−), and kept tags with the right classes", () => {
    const p: Preview = { kind: "tags", before: ["keep", "drop"], after: ["keep", "new"] };
    const { container } = renderWithProviders(<ApprovalView preview={p} />);

    // Sorted union: drop (removed), keep (kept), new (added).
    const chips = [...container.querySelectorAll(".tag-chip")];
    const byName = (n: string) => chips.find((c) => c.textContent?.includes(n))!;
    expect(byName("drop").className).toContain("removed");
    expect(byName("keep").className).toContain("kept");
    expect(byName("new").className).toContain("added");
    // The removed chip is prefixed "−", the added with "+".
    expect(byName("drop").textContent).toContain("−");
    expect(byName("new").textContent).toContain("+");
  });

  it("renders the (no tags) placeholder when both sides are empty", () => {
    const p: Preview = { kind: "tags", before: [], after: [] };
    renderWithProviders(<ApprovalView preview={p} />);
    expect(screen.getByText(/\(no tags\)/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------- //
// (c) fields / place table
// ---------------------------------------------------------------------------- //
describe("ApprovalView — fields/place", () => {
  it("renders a was→now table marking only the changed rows", () => {
    const p: Preview = {
      kind: "fields",
      before: { status: "draft", owner: "jeff" },
      after: { status: "done", owner: "jeff" },
    };
    const { container } = renderWithProviders(<ApprovalView preview={p} />);

    // Both keys present.
    expect(screen.getByText("status")).toBeInTheDocument();
    expect(screen.getByText("owner")).toBeInTheDocument();
    // Old + new values both shown.
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();

    // Only the status row carries the "changed" class.
    const rows = [...container.querySelectorAll("tr")];
    const statusRow = rows.find((r) => r.textContent?.includes("status"))!;
    const ownerRow = rows.find((r) => r.textContent?.includes("owner"))!;
    expect(statusRow.className).toContain("changed");
    expect(ownerRow.className).not.toContain("changed");
  });

  it("renders a create (before==null) with em-dash placeholders on the 'was' side", () => {
    const p: Preview = { kind: "place", before: null, after: { city: "Paris" } };
    const { container } = renderWithProviders(<ApprovalView preview={p} />);

    expect(screen.getByText("city")).toBeInTheDocument();
    expect(screen.getByText("Paris")).toBeInTheDocument();
    // before==null ⇒ the "was" cell is a muted em-dash placeholder.
    expect(container.querySelector(".was")?.textContent).toBe("—");
  });

  it("returns null for a missing preview", () => {
    // @ts-expect-error — exercising the null guard the component ships with.
    const { container } = renderWithProviders(<ApprovalView preview={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

// ---------------------------------------------------------------------------- //
// (d) exported helpers (consumed by StagingPanel headers)
// ---------------------------------------------------------------------------- //
describe("ApprovalView — helpers", () => {
  it("lineDiff is a set-difference of non-empty trimmed lines", () => {
    expect(lineDiff("a\nb\n", "b\nc")).toEqual({ removed: ["a"], added: ["c"] });
    expect(lineDiff("same", "same")).toEqual({ removed: [], added: [] });
  });

  it("previewStat summarises each kind", () => {
    expect(previewStat(null)).toBe("");
    expect(previewStat({ kind: "text", before: null, after: "x" })).toBe("new");
    expect(previewStat({ kind: "text", before: "x", after: null })).toBe("deletes this");
    expect(previewStat({ kind: "text", before: "a", after: "a\nb" })).toBe("+1/−0");
    expect(previewStat({ kind: "tags", before: ["x"], after: ["x", "y"] })).toBe("+1/−0 tag");
    expect(previewStat({ kind: "tags", before: ["x"], after: ["y", "z"] })).toBe("+2/−1 tags");
    expect(previewStat({ kind: "fields", before: { a: 1 }, after: { a: 2 } })).toBe("1 field changed");
  });
});
