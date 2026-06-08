import { type Page, expect } from "@playwright/test";
import { ACCESS_KEY } from "../playwright.config";

// Drive a fresh browser context from the access-key gate to the compose home.
// Each Playwright test gets a clean context (no stored key), so every spec connects.
// The owner-onboarding step only appears until an owner is set in the DB, so handle it
// optionally.
export async function connect(page: Page) {
  await page.goto("/");
  await page.getByPlaceholder(/Paste the access key/i).fill(ACCESS_KEY);
  await page.getByRole("button", { name: /^Connect$/ }).click();

  // After connect, EITHER owner-onboarding (first run only) OR the compose home appears.
  // Wait for whichever lands (isVisible() does NOT wait), then handle onboarding when
  // present (setting the owner exercises POST /api/people/owner).
  const ownerField = page.getByPlaceholder(/e\.g\./i);
  const fullBrain = page.getByRole("button", { name: "Full Brain" });
  await expect(ownerField.or(fullBrain).first()).toBeVisible();
  if (await ownerField.isVisible()) {
    await ownerField.fill("E2E Owner");
    await page.getByRole("button", { name: /Save & continue/i }).click();
  }
  await expect(fullBrain).toBeVisible();
}

// The main compose textarea on /chat.
export const composer = (page: Page) => page.locator("textarea").first();
export const sendBtn = (page: Page) => page.locator("button.icon-btn.send");
