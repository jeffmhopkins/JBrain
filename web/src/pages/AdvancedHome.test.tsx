// Component-integration (Tier 2) tests for AdvancedHome — the grouped Advanced nav
// grid. It's a static launcher: three sections (Knowledge / Authoring / System), each a
// grid of cards that navigate() on tap. No network. We assert the grouped cards render
// and that a tap navigates to the card's route.
//
// To observe navigation we render the grid inside a Routes with a sentinel destination
// route, so a real react-router navigate() swaps in the destination.
import { describe, expect, it } from "vitest";
import { Route, Routes } from "react-router-dom";
import { renderWithProviders, screen } from "../test/render";

import AdvancedHome from "./AdvancedHome";

function renderGrid() {
  return renderWithProviders(
    <Routes>
      <Route path="/" element={<AdvancedHome />} />
      <Route path="/wiki" element={<div>WIKI DESTINATION</div>} />
      <Route path="/system" element={<div>SYSTEM DESTINATION</div>} />
    </Routes>,
    { route: "/" },
  );
}

describe("AdvancedHome", () => {
  it("renders the three grouped sections", () => {
    renderGrid();
    // Section headers carry the .adv-section class (NB: "System" is also a card title,
    // so scope the assertion to the section header to avoid the duplicate match).
    const headers = document.querySelectorAll(".adv-section");
    const names = Array.from(headers).map((h) => h.textContent);
    expect(names).toEqual(["Knowledge", "Authoring", "System"]);
  });

  it("renders cards with their label + sub-label across sections", () => {
    renderGrid();
    // A Knowledge card.
    expect(screen.getByRole("button", { name: /Wiki/ })).toBeInTheDocument();
    expect(screen.getByText("Browse & edit notes")).toBeInTheDocument();
    // An Authoring card.
    expect(screen.getByRole("button", { name: /Triggers/ })).toBeInTheDocument();
    expect(screen.getByText("When actions run")).toBeInTheDocument();
    // A System card.
    expect(screen.getByRole("button", { name: /Data/ })).toBeInTheDocument();
    expect(screen.getByText("SQL · backup")).toBeInTheDocument();
  });

  it("navigates to a card's route when tapped (Knowledge → /wiki)", async () => {
    const { user } = renderGrid();
    await user.click(screen.getByRole("button", { name: /Wiki/ }));
    expect(await screen.findByText("WIKI DESTINATION")).toBeInTheDocument();
  });

  it("navigates from a different section too (System → /system)", async () => {
    const { user } = renderGrid();
    await user.click(screen.getByRole("button", { name: /^System/ }));
    expect(await screen.findByText("SYSTEM DESTINATION")).toBeInTheDocument();
  });
});
