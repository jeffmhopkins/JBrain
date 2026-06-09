import { test, expect } from "@playwright/test";
import { connect, composer, sendBtn } from "./helpers";
import { ACCESS_KEY } from "../playwright.config";

// REGRESSION for "research goes unresponsive after opening Edit with AI": opening the
// conversational KB editor and CLOSING it must not wedge the live chat. Real PWA -> API ->
// faked LLM. The fix: suggest-mode gather opens straight to the curate screen from the
// deterministic seeds (no agentic loop, no entity rebind), so nothing holds the WAL write
// lock that previously blocked the next chat turn's message INSERT.
test("Edit with AI: open then Close leaves Research responsive", async ({ page, request, baseURL }) => {
  await connect(page);

  const marker = `e2e-pal-${Date.now()}`;
  const res = await request.post(`${baseURL}/api/notes`, {
    headers: { Authorization: `Bearer ${ACCESS_KEY}` },
    data: { title: `kb/People/${marker}`, content_md: `# ${marker}\n\nPal is a friend.\n` },
  });
  expect(res.ok()).toBeTruthy();
  const { slug } = await res.json();

  await page.goto(`/note/${slug}`);
  await expect(page.getByRole("heading", { name: marker })).toBeVisible();

  // Open Edit with AI — suggest mode opens straight to the source-picker (no "Gathering context…"
  // stall). The Start/Edit button is the curate screen being ready.
  await page.getByRole("button", { name: "Page actions" }).click();
  await page.getByRole("menuitem", { name: /Edit with AI/ }).click();
  const startBtn = page.getByRole("button", { name: /Start editing|Edit with \d+ source/ });
  await expect(startBtn).toBeVisible({ timeout: 30_000 });

  // Close the panel.
  await page.getByRole("button", { name: "Close" }).click();
  await expect(startBtn).not.toBeVisible();

  // Research must STILL stream a reply — this is the interaction that regressed.
  await page.goto("/");
  await page.getByRole("button", { name: "Research" }).click();
  await composer(page).fill("What do you know about pelicans?");
  await sendBtn(page).click();
  await expect(page.getByText(/deterministic end-to-end test reply/i)).toBeVisible({ timeout: 15_000 });
});

// The status-panel "Reset AI" recovery control: drops cached provider clients + cancels rebuild
// runs server-side, and the chat keeps working afterwards.
test("Reset AI from the status panel keeps Research working", async ({ page }) => {
  await connect(page);

  await page.locator(".status-dot-btn").click();                 // open the status panel
  await page.getByRole("button", { name: /Reset AI/i }).click();
  await expect(page.getByRole("button", { name: /Reset AI/i })).toBeVisible();   // completes (not stuck on "Resetting…")
  await page.keyboard.press("Escape");                           // close the panel

  await page.getByRole("button", { name: "Research" }).click();
  await composer(page).fill("hello there");
  await sendBtn(page).click();
  await expect(page.getByText(/deterministic end-to-end test reply/i)).toBeVisible({ timeout: 15_000 });
});
