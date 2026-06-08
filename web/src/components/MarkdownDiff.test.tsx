// Component-integration (Tier 2) tests for MarkdownDiff — the shared "rendered diff"
// that shows a note's body with full markdown formatting and the change highlighted
// in place (green/jb-block-ins = added, red/jb-block-del = removed). It's props-only
// and pure (no fetch), but it calls useNavigate() (via makeLinkRenderer) so it renders
// under the router via renderWithProviders.
//
//   before == null → brand-new note (whole body = an addition, .jb-all-add)
//   after  == null → deletion        (whole body struck through, .jb-all-del)
//   otherwise       → an in-place line diff (rehypeDiff tags added/removed blocks)
import { afterEach, describe, expect, it } from "vitest";
import { renderWithProviders, screen, cleanup } from "../test/render";

import MarkdownDiff from "./MarkdownDiff";

afterEach(cleanup);

describe("MarkdownDiff", () => {
  it("renders a brand-new note (before==null) as a whole-body addition", () => {
    const { container } = renderWithProviders(
      <MarkdownDiff before={null} after={"# Sourdough\n\nFeed the starter daily."} />,
    );
    // Body content renders…
    expect(screen.getByText(/Feed the starter daily/)).toBeInTheDocument();
    // …and the root carries the all-add class (whole note is new).
    expect(container.querySelector(".md-diff.jb-all-add")).not.toBeNull();
    expect(container.querySelector(".jb-all-del")).toBeNull();
  });

  it("renders a deletion (after==null) as a whole-body removal", () => {
    const { container } = renderWithProviders(
      <MarkdownDiff before={"# Obsolete\n\nstale body"} after={null} />,
    );
    expect(screen.getByText(/stale body/)).toBeInTheDocument();
    expect(container.querySelector(".md-diff.jb-all-del")).not.toBeNull();
    expect(container.querySelector(".jb-all-add")).toBeNull();
  });

  it("renders an in-place edit tagging added and removed lines, keeping unchanged ones plain", () => {
    const { container } = renderWithProviders(
      <MarkdownDiff before={"Milk\nEggs\nDrop me"} after={"Milk\nEggs\nButter"} />,
    );
    // Kept lines and both sides of the change are present in the merged document.
    expect(screen.getByText(/Milk/)).toBeInTheDocument();
    expect(screen.getByText(/Butter/)).toBeInTheDocument();
    // rehypeDiff tags the changed blocks; an unchanged "Milk" paragraph is neither.
    const insBlock = container.querySelector(".jb-block-ins");
    const delBlock = container.querySelector(".jb-block-del");
    expect(insBlock).not.toBeNull();
    expect(delBlock).not.toBeNull();
    // The whole-body add/del classes are NOT applied for an in-place diff.
    expect(container.querySelector(".jb-all-add")).toBeNull();
    expect(container.querySelector(".jb-all-del")).toBeNull();
  });

  it("shows 'No textual change.' when both sides are identical (diff mode, unchanged)", () => {
    renderWithProviders(<MarkdownDiff before={"same body"} after={"same body"} />);
    expect(screen.getByText(/No textual change\./i)).toBeInTheDocument();
    expect(screen.getByText("same body")).toBeInTheDocument();
  });

  it("passes through an extra className", () => {
    const { container } = renderWithProviders(
      <MarkdownDiff before={"a"} after={"a"} className="extra-cls" />,
    );
    expect(container.querySelector(".md-diff.extra-cls")).not.toBeNull();
  });
});
