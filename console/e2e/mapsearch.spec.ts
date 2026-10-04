import { expect, test } from "@playwright/test";
import { seedTopics } from "./seed";

test("searching the map rings the hits and opens the best one", async ({ page, request }) => {
  await seedTopics(request);
  await page.goto("/map");
  await expect(page.getByTestId("map-node-count")).not.toHaveText("0");
  const input = page.getByTestId("map-search-input");
  await input.fill("payout export commission deposits");
  await input.press("Enter");
  await expect(page.getByTestId("map-search-hit-count")).not.toHaveText("0");
  const pane = page.getByTestId("reading-pane");
  await expect(pane.getByRole("heading", { name: /Payout export \d/ })).toBeVisible();
  const results = page.getByTestId("map-search-results").getByRole("button");
  await expect(results.first()).toContainText("Payout export");
  await page.getByRole("button", { name: "Clear search" }).click();
  await expect(page.getByTestId("map-search-results")).toHaveCount(0);
});
