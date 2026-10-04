import { expect, test } from "@playwright/test";

test("map explains an empty tenant instead of showing a blank canvas", async ({ page }) => {
  await page.goto("/map");
  await expect(page.getByRole("heading", { name: "Memory map" })).toBeVisible();
  const picker = page.getByLabel("Tenant");
  await expect(picker.locator("option", { hasText: "Empty Org" })).toHaveCount(1);
  await picker.selectOption({ label: "Empty Org · 0 memories" });
  const empty = page.getByTestId("map-empty");
  await expect(empty).toBeVisible();
  await expect(empty).toContainText("No memories in Empty Org yet");
  await expect(page.getByTestId("map-node-count")).toHaveText("0");
});

test("tenant picker shows names, not id fragments", async ({ page }) => {
  await page.goto("/map");
  await expect(page.getByLabel("Tenant").locator("option", { hasText: /^E2E/ })).toHaveCount(1);
});
