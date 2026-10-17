import { expect, test } from "@playwright/test";

test("live feed shows a search trace", async ({ page, request }) => {
  const api = process.env.ALBERT_API_URL ?? "http://127.0.0.1:18080";
  await page.goto("/live");
  await expect(page.getByRole("heading", { name: "Live" })).toBeVisible();
  const response = await request.post(`${api}/v1/search`, {
    headers: { Authorization: `Bearer ${process.env.ALBERT_E2E_KEY}` },
    data: { query: "playwright live probe" },
  });
  expect(response.ok()).toBeTruthy();
  const row = page.getByTestId("trace-row").filter({ hasText: "POST /v1/search" }).first();
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(row).toContainText("playwright live probe");
});

test("an agent search pulses the memories it recalled on the open map", async ({ page, request }) => {
  const { headers, seedTopics } = await import("./seed");
  const api = process.env.ALBERT_API_URL ?? "http://127.0.0.1:18080";
  await seedTopics(request);
  await page.goto("/map");
  await expect(page.getByTestId("map-live-state")).toHaveText("live", { timeout: 15_000 });
  await expect(page.getByTestId("map-pulse-count")).toHaveText("0");
  const response = await request.post(`${api}/v1/search`, {
    headers,
    data: { query: "brand voice sharp confident" },
  });
  expect(response.ok()).toBeTruthy();
  await expect(page.getByTestId("map-pulse-count")).not.toHaveText("0", { timeout: 10_000 });
  await expect(page.getByTestId("map-ticker")).toContainText("brand voice sharp confident");

  await page.getByRole("button", { name: "Replay" }).click();
  await expect(page.getByTestId("map-live-state")).toHaveText("replay");
  await expect(page.getByTestId("replay-event-count")).not.toHaveText("0", { timeout: 10_000 });
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await expect(page.getByRole("button", { name: "Pause" })).toBeVisible();
});

test("the live map recovers from a transient stream connection failure", async ({ page, request }) => {
  const { seedTopics } = await import("./seed");
  await seedTopics(request);
  let attempts = 0;
  await page.route("**/api/albert/v1/console/stream", async (route) => {
    attempts += 1;
    if (attempts === 1) await route.abort("connectionfailed");
    else await route.continue();
  });

  await page.goto("/map");
  await expect(page.getByTestId("map-live-state")).toHaveText("live", { timeout: 20_000 });
  expect(attempts).toBe(2);
});

test("the live map bounds persistent stream connection retries", async ({ page, request }) => {
  const { seedTopics } = await import("./seed");
  await seedTopics(request);
  let attempts = 0;
  await page.route("**/api/albert/v1/console/stream", async (route) => {
    attempts += 1;
    await route.abort("connectionfailed");
  });

  await page.goto("/map");
  await expect.poll(() => attempts, { timeout: 10_000 }).toBe(4);
  await expect(page.getByTestId("map-live-state")).toHaveText("paused");
  await page.waitForTimeout(1_000);
  expect(attempts).toBe(4);
});
