import { expect, test } from "@playwright/test";

test("live feed shows a search trace", async ({ page, request }) => {
  const api = process.env.ALBERT_API_URL ?? "http://127.0.0.1:18080";
  await page.goto("/");
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
