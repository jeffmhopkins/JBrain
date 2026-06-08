import { test, expect } from "@playwright/test";
import { connect, composer, sendBtn } from "./helpers";

// The highest-value journey: the Full Brain architect's propose -> Apply path. In Full
// Brain mode the chat turn offers the `propose_actions` tool; the fake LLM answers with a
// single CREATE action (see fake_llm.py), which the server stages. The staging area then
// renders an Apply affordance; applying it writes the note through the real
// API -> SQLite, records a "✓ Created …" approval chip in the chat, and the note becomes
// findable via search — proving the whole propose -> stage -> apply -> persist loop.
test("Full Brain: propose_actions -> staging Apply creates the note", async ({ page }) => {
  await connect(page);

  // Full Brain (= the assisted architect) is the only mode that offers propose_actions.
  await page.getByRole("button", { name: "Full Brain" }).click();
  await composer(page).fill("Capture a note about pelicans for me.");
  await sendBtn(page).click();

  // The staged proposal lands as a pending card with an Apply button (the StagingPanel
  // reloads /api/staging when the stream emits its `staging` event). Wait for the Apply
  // button — web-first, so this also waits out the stream + the staging reload.
  const applyBtn = page.getByRole("button", { name: "Apply", exact: true });
  await expect(applyBtn.first()).toBeVisible({ timeout: 20_000 });

  // Apply the proposal. The server creates the note and records a "✓ Created …" approval
  // chip back into the conversation.
  await applyBtn.first().click();

  // The applied chip confirms the write round-tripped (it's reloaded from the server as a
  // persisted 'event' row, not just optimistic UI).
  await expect(page.getByText(/Created/i).first()).toBeVisible({ timeout: 15_000 });

  // And the proposed note is now in the brain: find it via hybrid search and open it.
  await page.goto("/search?q=E2E%20Proposed%20Note");
  const noteLink = page.locator("a[href^='/note/']").first();
  await expect(noteLink).toBeVisible({ timeout: 15_000 });
  await noteLink.click();
  await expect(page).toHaveURL(/\/note\//);
  await expect(page.getByText(/E2E Proposed Note/i).first()).toBeVisible();
});
