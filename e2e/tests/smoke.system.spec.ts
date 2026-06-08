import { test, expect } from "@playwright/test";
import { ACCESS_KEY } from "../playwright.config";

// The canonical first-run journey: a fresh device loads the PWA, is gated by the
// access-key entry, pastes the key, and lands on the compose home. This exercises the
// real PWA <-> API auth handshake end-to-end (no mocks beyond the LLM boundary).
test("first run: access-key gate -> compose home", async ({ page }) => {
  await page.goto("/");

  // KeyEntry gate: the connect prompt + the access-key field + Connect button.
  await expect(page.getByText(/Connect this device to your brain/i)).toBeVisible();
  const keyField = page.getByPlaceholder(/Paste the access key/i);
  await expect(keyField).toBeVisible();

  await keyField.fill(ACCESS_KEY);
  await page.getByRole("button", { name: /^Connect$/ }).click();

  // After connect, EITHER owner-onboarding (first run) OR the compose home appears.
  // Wait for whichever lands, then skip onboarding if shown.
  const later = page.getByRole("button", { name: /do this later/i });
  const fullBrain = page.getByRole("button", { name: "Full Brain" });
  await expect(later.or(fullBrain).first()).toBeVisible();
  if (await later.isVisible()) await later.click();

  // Landed on the compose home: the 3-mode segmented control is present, and the
  // gate is gone (verify() succeeded against the real API).
  await expect(fullBrain).toBeVisible();
  await expect(page.getByRole("button", { name: "Research" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Entry" })).toBeVisible();
  await expect(page.getByPlaceholder(/Paste the access key/i)).toHaveCount(0);
});
