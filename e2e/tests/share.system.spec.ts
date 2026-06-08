import { test, expect } from "@playwright/test";
import { connect, composer, sendBtn } from "./helpers";

// The public-share journey: capture a note, mint a VIEW-ONLY public link from its page,
// then open that link in a SECOND, key-less browser context and confirm the note renders
// read-only on /share/:token. This proves the recipient path — a stranger with just the
// URL (no access key) sees the content but gets no edit affordance. A plain view link is
// not device-bound (bind defaults off for a non-private note), so the second context can
// open it directly without the claim/consent gate.
test("mint a view-only share link -> recipient sees it read-only", async ({ page, browser }) => {
  await connect(page);

  const marker = `osprey-${Date.now()}`;

  // Capture a note via Entry (no AI), then follow the Saved chip to its page.
  await page.getByRole("button", { name: "Entry" }).click();
  await composer(page).fill(`Shareable note about ${marker}`);
  await sendBtn(page).click();
  const savedChip = page.locator("a.saved-chip");
  await expect(savedChip).toBeVisible();
  await savedChip.click();
  await expect(page).toHaveURL(/\/note\//);
  await expect(page.getByText(new RegExp(marker, "i")).first()).toBeVisible();

  // Open the kebab "Page actions" menu and choose Share to reveal the share panel.
  await page.getByRole("button", { name: /Page actions/i }).click();
  await page.getByRole("menuitem", { name: /^Share$/ }).click();

  // The panel defaults to "View only"; mint the link.
  await expect(page.getByRole("button", { name: /Create link/i })).toBeVisible();
  await page.getByRole("button", { name: /Create link/i }).click();

  // The minted URL lands in the read-only share-url field. Read it out of the DOM.
  const urlField = page.locator("input.share-url");
  await expect(urlField).toBeVisible();
  const shareUrl = await urlField.inputValue();
  expect(shareUrl).toMatch(/\/share\//);
  // The server mints an absolute URL from JBRAIN_DOMAIN (http://localhost/share/<token>),
  // but the test server actually listens on baseURL (127.0.0.1:<port>). Navigate by the
  // /share/:token PATH so we hit the server under test, not localhost:80.
  const sharePath = shareUrl.slice(shareUrl.indexOf("/share/"));
  // Rebuild it against the origin the test server is actually reachable on (taken from the
  // owner page's current URL — i.e. the configured baseURL).
  const origin = new URL(page.url()).origin;
  const recipientUrl = origin + sharePath;

  // Open the link in a fresh context (no stored access key, like a real recipient).
  const recipient = await browser.newContext();
  try {
    const guest = await recipient.newPage();
    await guest.goto(recipientUrl);

    // The shared note renders read-only: the body content is visible, it's badged
    // read-only, and there's no edit affordance ("Suggest an edit" is edit-scope only).
    await expect(guest.getByText(new RegExp(marker, "i")).first()).toBeVisible({ timeout: 15_000 });
    await expect(guest.getByText(/Shared · read-only/i)).toBeVisible();
    await expect(guest.getByRole("button", { name: /Suggest an edit/i })).toHaveCount(0);
    // And the recipient was never asked for the access key (public route is key-less).
    await expect(guest.getByPlaceholder(/Paste the access key/i)).toHaveCount(0);
  } finally {
    await recipient.close();
  }
});
