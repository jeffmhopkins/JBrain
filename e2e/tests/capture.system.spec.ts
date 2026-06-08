import { test, expect } from "@playwright/test";
import { connect, composer, sendBtn } from "./helpers";

// Canonical write journey: capture a note via Entry mode (no AI), then find it again
// through hybrid search — exercising the full PWA -> API -> SQLite -> FTS round-trip.
test("Entry capture -> searchable in the brain", async ({ page }) => {
  await connect(page);

  const marker = `pelican-${Date.now()}`;

  // Entry mode is a straight write (no LLM).
  await page.getByRole("button", { name: "Entry" }).click();
  await composer(page).fill(`E2E capture about ${marker}`);
  await sendBtn(page).click();

  // The optimistic "Saved" confirmation chip links to the new note. Follow it.
  const savedChip = page.locator("a.saved-chip");
  await expect(savedChip).toBeVisible();
  await savedChip.click();

  // On the note page, the body we captured is loaded back from the server — proving the
  // write round-tripped through the API + SQLite, not just optimistic UI.
  await expect(page).toHaveURL(/\/note\//);
  await expect(page.getByText(new RegExp(marker, "i")).first()).toBeVisible();

  // And it's findable via hybrid search (keyword/FTS).
  await page.goto(`/search?q=${marker}`);
  await expect(page.locator("a[href^='/note/']").first()).toBeVisible();
});
