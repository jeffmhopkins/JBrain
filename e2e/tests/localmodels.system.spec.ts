import { test, expect } from "@playwright/test";
import { connect } from "./helpers";

// Local-LLM (Ollama) model management, end-to-end against the real stack with Ollama
// faked at the admin boundary (fake_llm.py serves /api/tags, /api/pull, /api/delete).
// The journey: open System settings → the "Local models (Ollama)" card shows the
// running state with an empty install list and a curated one-click Pull → pull
// qwen2.5:7b (SSE progress, then the list refreshes to show it installed) → assign that
// local model to the "Routine jobs" tier in the Model card → reload → the assignment
// persisted (the PUT to /api/prompts/models.cheap stuck).
test("local models: pull a curated Ollama model and assign it to a task tier", async ({ page }) => {
  await connect(page);

  // Reach System settings: Advanced launcher (bolt) → "System" card.
  await page.getByRole("button", { name: /Advanced/i }).click();
  await page.getByRole("button", { name: /^System/ }).click();

  // The "Local models (Ollama)" card is in its running state: no installed models yet,
  // and the curated "Qwen2.5 7B" is offered for one-click pull (its Pull button enabled
  // because a 7B model fits on any CI runner's RAM).
  const card = page.locator(".card").filter({ hasText: "Local models (Ollama)" });
  await expect(card.getByRole("heading", { name: "Local models (Ollama)" })).toBeVisible();
  await expect(card.getByText(/No local models installed yet/i)).toBeVisible();

  const qwenRow = card.locator(".row").filter({ hasText: "Qwen2.5 7B" });
  const pullBtn = qwenRow.getByRole("button", { name: /^Pull$/ });
  await expect(pullBtn).toBeEnabled();

  // Pull it. The fake streams a short SSE; on the terminal "done" the panel re-GETs the
  // list, so qwen2.5:7b appears as an installed model (with a Remove button).
  await pullBtn.click();
  await expect(card.getByText("qwen2.5:7b")).toBeVisible({ timeout: 20_000 });
  await expect(card.locator(".local-model-list").getByRole("button", { name: /^Remove$/ }))
    .toBeVisible();

  // Assign the newly-installed local model to the "Routine jobs" tier (models.cheap).
  // The Model card's per-tier <select> now carries a "Local (Ollama)" optgroup whose
  // option value is the bare Ollama id ("qwen2.5:7b").
  const modelCard = page.locator(".card").filter({ has: page.getByText("Model", { exact: true }) });
  const routineSelect = modelCard.locator("label.model-row")
    .filter({ hasText: "Routine jobs" }).locator("select");
  await routineSelect.selectOption("qwen2.5:7b");
  await expect(routineSelect).toHaveValue("qwen2.5:7b");

  // Reload: the assignment is server-side (PUT /api/prompts/models.cheap), so it should
  // survive a fresh load of the page. Re-navigate through the launcher and re-read.
  await page.reload();
  await page.getByRole("button", { name: /Advanced/i }).click();
  await page.getByRole("button", { name: /^System/ }).click();

  const routineSelectAfter = page.locator(".card").filter({ has: page.getByText("Model", { exact: true }) })
    .locator("label.model-row").filter({ hasText: "Routine jobs" }).locator("select");
  await expect(routineSelectAfter).toHaveValue("qwen2.5:7b");
});
