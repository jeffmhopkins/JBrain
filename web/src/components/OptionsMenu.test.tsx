// Component (Tier 2) tests for the "⋮" actions dropdown (NoteActionsMenu) — the
// single options menu that replaces the inline Share/Edit/Delete buttons. Props-only:
// it takes a list of MenuItem rows + callbacks, so no network is touched.
//
// Covered: closed-by-default render of just the trigger; opening reveals the rows +
// separators; clicking a row fires its onClick AND closes the menu; outside-click and
// Escape close it (Escape returns focus to the trigger); accent/danger classes from
// props; empty/sep-only edge.
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders, screen, fireEvent } from "../test/render";
import NoteActionsMenu, { type MenuItem } from "./NoteActionsMenu";

const items = (over: Partial<Record<string, () => void>> = {}): MenuItem[] => [
  { key: "share", label: "Share", icon: "share", accent: true, onClick: over.share ?? (() => {}) },
  { key: "edit", label: "Edit", icon: "edit", hint: "⌘E", onClick: over.edit ?? (() => {}) },
  { sep: true },
  { key: "delete", label: "Delete", icon: "trash", danger: true, onClick: over.delete ?? (() => {}) },
];

describe("OptionsMenu (NoteActionsMenu)", () => {
  it("renders only the trigger collapsed, then the rows + separator once opened", () => {
    renderWithProviders(<NoteActionsMenu items={items()} />);
    const trigger = screen.getByRole("button", { name: "Page actions" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const menu = screen.getByRole("menu");
    expect(menu).toBeInTheDocument();
    expect(screen.getAllByRole("menuitem")).toHaveLength(3); // share, edit, delete
    expect(menu.querySelector(".note-menu-sep")).toBeTruthy();
    expect(screen.getByText("⌘E")).toBeInTheDocument(); // the edit hint
  });

  it("clicking a row fires its callback and closes the menu", () => {
    const onDelete = vi.fn();
    renderWithProviders(<NoteActionsMenu items={items({ delete: onDelete })} />);
    fireEvent.click(screen.getByRole("button", { name: "Page actions" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete/ }));
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("applies accent and danger classes from the item props", () => {
    renderWithProviders(<NoteActionsMenu items={items()} />);
    fireEvent.click(screen.getByRole("button", { name: "Page actions" }));
    expect(screen.getByRole("menuitem", { name: /Share/ }).className).toContain("accent");
    expect(screen.getByRole("menuitem", { name: /Delete/ }).className).toContain("danger");
  });

  it("closes on an outside mousedown without firing any callback", () => {
    const onShare = vi.fn();
    renderWithProviders(<NoteActionsMenu items={items({ share: onShare })} />);
    fireEvent.click(screen.getByRole("button", { name: "Page actions" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(onShare).not.toHaveBeenCalled();
  });

  it("Escape closes the menu and returns focus to the trigger", () => {
    renderWithProviders(<NoteActionsMenu items={items()} label="Stuff" />);
    const trigger = screen.getByRole("button", { name: "Stuff" });
    fireEvent.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });

  it("renders an open menu with no actionable rows when given only a separator", () => {
    renderWithProviders(<NoteActionsMenu items={[{ sep: true }]} />);
    fireEvent.click(screen.getByRole("button", { name: "Page actions" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(screen.queryAllByRole("menuitem")).toHaveLength(0);
    expect(screen.getByRole("separator")).toBeInTheDocument();
  });
});
