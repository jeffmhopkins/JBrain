import { test, expect } from "@playwright/test";
import { connect } from "./helpers";
import { ACCESS_KEY } from "../playwright.config";

// The trigger/automation journey: a workflow's "Run now" executes its action, and any
// review it posts shows up in the Review inbox at /review. We seed a deterministic,
// LLM-free workflow whose action is the `create_review_item` recipe (a single
// unconditional `create_review` step — see actions/create_review_item.yaml), so the
// Run-now -> Review path is exercisable without depending on the fake LLM or pre-existing
// DB state. The workflow is created via the authed API (same Bearer key the PWA uses),
// then driven through the real /flows UI.
test("Run now on a workflow -> a Review card appears at /review", async ({ page, request, baseURL }) => {
  await connect(page);

  const marker = `flowcard-${Date.now()}`;

  // Seed a deterministic review-posting workflow via the authenticated API. The
  // `create_review_item` action is a single unconditional `create_review` step, so running
  // it just posts the configured card — no LLM, no input notes required.
  const res = await request.post(`${baseURL}/api/workflows`, {
    headers: { Authorization: `Bearer ${ACCESS_KEY}` },
    data: {
      name: `E2E ${marker}`,
      trigger_type: "schedule",
      trigger_config: { cron: "0 0 * * *" },
      action_type: "create_review_item",
      action_config: { title: marker, message: "Posted by the e2e run-now journey." },
      enabled: true,
    },
  });
  expect(res.ok()).toBeTruthy();

  // Drive the real Triggers UI: find our workflow's card and hit "Run now".
  await page.goto("/flows");
  const card = page.locator(".card").filter({ hasText: `E2E ${marker}` });
  await expect(card).toBeVisible({ timeout: 15_000 });
  await card.getByRole("button", { name: "Run now" }).click();

  // Run-now opens a "watch" modal that ends in "Completed"; the run is synchronous-ish and
  // posts the review card. Close the modal once the run settles (web-first wait).
  await expect(page.getByText(/Completed/i).first()).toBeVisible({ timeout: 20_000 });

  // The posted card is now in the Review inbox.
  await page.goto("/review");
  await expect(page.getByText(marker).first()).toBeVisible({ timeout: 15_000 });
});
