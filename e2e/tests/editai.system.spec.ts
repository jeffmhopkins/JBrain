import { test, expect } from "@playwright/test";
import { connect } from "./helpers";
import { ACCESS_KEY } from "../playwright.config";

// The KB "Edit with AI" journey: seed a KB article, open the conversational edit panel,
// talk once, watch the (faked) revision stream in, and Accept it onto the page. The LLM is
// faked at the boundary — fake_llm returns a full article body for the suggest/edit turn — so
// this exercises the REAL gather -> curate -> suggest -> accept pipeline end to end, with no
// production-code mocks. Sources are optional in suggest mode, so the gather producing none
// (the fake proposes nothing) still lets the owner start the conversation.
test("Edit with AI: talk -> edit -> Accept persists the revised article", async ({ page, request, baseURL }) => {
  await connect(page);

  const marker = `e2e-pal-${Date.now()}`;
  // Seed a KB article via the authed API. A title under kb/ is inferred as kind=kb, so the
  // note's "Edit with AI" action appears.
  const res = await request.post(`${baseURL}/api/notes`, {
    headers: { Authorization: `Bearer ${ACCESS_KEY}` },
    data: { title: `kb/People/${marker}`, content_md: `# ${marker}\n\nPal is a friend.\n` },
  });
  expect(res.ok()).toBeTruthy();
  const { slug } = await res.json();

  await page.goto(`/note/${slug}`);
  await expect(page.getByRole("heading", { name: marker })).toBeVisible();

  // Open the kebab actions menu -> Edit with AI (opens the unified panel in suggest mode).
  await page.getByRole("button", { name: "Page actions" }).click();
  await page.getByRole("menuitem", { name: /Edit with AI/ }).click();

  // Gather runs, then the curate stage offers "Start editing" (sources optional in suggest mode).
  const startBtn = page.getByRole("button", { name: /Start editing|Edit with \d+ source/ });
  await expect(startBtn).toBeVisible({ timeout: 30_000 });
  await startBtn.click();

  // Talk once: the first message opens the edit loop (POST /suggest).
  const box = page.getByPlaceholder(/Tell me what to change/i);
  await expect(box).toBeVisible();
  await box.fill("Mention that Pal enjoys sailing.");
  await page.locator("button.rb-send").click();

  // The faked revision streams in; reveal the plain draft and confirm the new content.
  await expect(page.getByRole("button", { name: /^Accept$/ })).toBeVisible({ timeout: 30_000 });
  const hide = page.getByRole("button", { name: /Hide changes/ });
  if (await hide.isVisible()) await hide.click();
  await expect(page.getByText(/revised by the end-to-end fake model/i)).toBeVisible();

  // Accept -> the panel closes and the page reloads showing the revised article.
  await page.getByRole("button", { name: /^Accept$/ }).click();
  await expect(page.getByText(/revised by the end-to-end fake model/i)).toBeVisible({ timeout: 30_000 });
});
