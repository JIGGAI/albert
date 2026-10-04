import { expect, test } from "@playwright/test";
import { seedTopics } from "./seed";

test("map shows titled clusters, typed links and a workspace picker", async ({ page, request }) => {
  await seedTopics(request);
  await page.goto("/map");
  await expect(page.getByRole("heading", { name: "Memory map" })).toBeVisible();
  await expect(page.getByTestId("map-canvas").locator("canvas")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("map-node-count")).not.toHaveText("0");
  await expect(page.getByTestId("map-link-count")).not.toHaveText("0");
  await expect(page.getByTestId("map-cluster-count")).not.toHaveText("0");
  const clusters = page.getByTestId("map-cluster-list");
  await expect(clusters).toContainText("Brand Voice");
  await expect(clusters).toContainText("Payout Export");
  await expect(page.getByLabel("Workspace")).toBeVisible();
  await expect(page.getByLabel("Workspace").locator("option", { hasText: "Default" })).toHaveCount(1);
  await expect(page.getByTestId("map-legend")).toBeVisible();
  // The counts read as one line of text, not a stack of separate boxes.
  const stats = page.locator(".map-stats");
  const box = await stats.boundingBox();
  expect(box?.height ?? 99).toBeLessThan(30);
  await expect(stats).toContainText(/\d+ memories, \d+ links, \d+ clusters/);
});

test("switching a link kind off changes the link count", async ({ page, request }) => {
  await seedTopics(request);
  await page.goto("/map");
  const count = page.getByTestId("map-link-count");
  await expect(count).not.toHaveText("0");
  const before = Number(await count.textContent());
  await page.getByRole("checkbox", { name: "Similar" }).uncheck();
  await expect(count).not.toHaveText(String(before));
  expect(Number(await count.textContent())).toBeLessThan(before);
});

test("the old explorer address lands on the map", async ({ page }) => {
  await page.goto("/explorer");
  await expect(page).toHaveURL(/\/map$/);
});
