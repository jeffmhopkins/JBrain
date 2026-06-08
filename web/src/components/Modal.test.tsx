// Component-integration (Tier 2) tests for Modal — the one responsive modal for the
// whole app. Props-only and pure (no fetch): it portals to <body>, wires Escape +
// backdrop-click to onClose, focuses itself on mount, marks <body class="modal-open">,
// and renders title / headerExtra / children / optional footer. No router needed (it
// uses no Link/navigate), so plain render is enough.
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup, fireEvent } from "../test/render";
import userEvent from "@testing-library/user-event";

import Modal from "./Modal";

afterEach(cleanup);

describe("Modal", () => {
  it("renders title, children and footer, portalled into <body> with modal-open set", () => {
    render(
      <Modal title="My Title" onClose={() => {}} footer={<button>Save</button>}>
        <p>Body content</p>
      </Modal>,
    );
    expect(screen.getByText("My Title")).toBeInTheDocument();
    expect(screen.getByText("Body content")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    // It's an accessible dialog, focused on mount, with <body> locked.
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveFocus();
    expect(document.body).toHaveClass("modal-open");
  });

  it("omits the footer when none is given and renders headerExtra", () => {
    const { container } = render(
      <Modal title="T" onClose={() => {}} headerExtra={<span className="badge">src</span>}>
        body
      </Modal>,
    );
    expect(container.querySelector(".modal-foot")).toBeNull();
    expect(screen.getByText("src")).toBeInTheDocument();
  });

  it("calls onClose when the Close button is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Modal title="T" onClose={onClose}>body</Modal>);
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when the backdrop is clicked but NOT when the card is clicked", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Modal title="T" onClose={onClose}>body</Modal>);
    // Clicking inside the card is stopped from propagating to the backdrop.
    await user.click(screen.getByText("body"));
    expect(onClose).not.toHaveBeenCalled();
    // Clicking the backdrop itself closes (fire directly on the backdrop element,
    // since a centered click would land on the card it contains).
    fireEvent.click(document.querySelector(".modal-backdrop")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose on the Escape key", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<Modal title="T" onClose={onClose}>body</Modal>);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("applies wide / compact size classes", () => {
    // Modal portals to <body>, so query the document, not the render container.
    const { rerender } = render(<Modal title="T" onClose={() => {}} size="wide">b</Modal>);
    expect(document.querySelector(".modal.modal-wide")).not.toBeNull();
    rerender(<Modal title="T" onClose={() => {}} size="compact">b</Modal>);
    expect(document.querySelector(".modal-backdrop.compact")).not.toBeNull();
    expect(document.querySelector(".modal.modal-compact")).not.toBeNull();
  });

  it("removes modal-open from <body> on unmount", () => {
    const { unmount } = render(<Modal title="T" onClose={() => {}}>b</Modal>);
    expect(document.body).toHaveClass("modal-open");
    unmount();
    expect(document.body).not.toHaveClass("modal-open");
  });
});
