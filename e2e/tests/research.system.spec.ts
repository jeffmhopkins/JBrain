import { test, expect } from "@playwright/test";
import { connect, composer, sendBtn } from "./helpers";

// Canonical read-only AI journey: Research mode asks a question, the server streams the
// answer from the (mocked-at-the-boundary) LLM, and the reply renders. This drives the
// real SSE chat path PWA -> API -> OpenAI-compatible fake.
test("Research mode streams an answer from the (faked) LLM", async ({ page }) => {
  await connect(page);

  // Research is the default mode, but click it to be explicit.
  await page.getByRole("button", { name: "Research" }).click();
  await composer(page).fill("What do you know about pelicans?");
  await sendBtn(page).click();

  // The fake LLM's deterministic reply streams back into the transcript.
  await expect(page.getByText(/deterministic end-to-end test reply/i)).toBeVisible({
    timeout: 15_000,
  });
});
